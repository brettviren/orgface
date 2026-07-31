;;; bootstrap.el --- daemon-side support for org-cli-helper  -*- lexical-binding: t; -*-

;; Loaded into the managed `emacs --daemon' by org-cli-helper.  Every
;; function returns a base64-encoded UTF-8 JSON string so the CLI never has
;; to parse Emacs' printed representation of anything richer than a string.

;;; Code:

(require 'org-capture)

(defconst org-cli-helper-version "1"
  "Protocol version, probed by the CLI to detect a bootstrapped daemon.")

(defvar org-cli-helper--expiry-timer nil
  "Timer that shuts this daemon down once it has been idle long enough.")

(defun org-cli-helper--payload (object)
  "Encode OBJECT as base64 UTF-8 JSON."
  (base64-encode-string
   (encode-coding-string (json-serialize object) 'utf-8)
   t))

(defun org-cli-helper-expire ()
  "Save anything outstanding and shut the daemon down."
  (ignore-errors (save-some-buffers t))
  (kill-emacs 0))

(defun org-cli-helper-touch (seconds)
  "Restart the idle countdown, exiting after SECONDS of quiet.
A nil or non-positive SECONDS cancels expiry entirely."
  (when (timerp org-cli-helper--expiry-timer)
    (cancel-timer org-cli-helper--expiry-timer))
  (setq org-cli-helper--expiry-timer
        (when (and (numberp seconds) (> seconds 0))
          (run-at-time seconds nil #'org-cli-helper-expire)))
  nil)

(defun org-cli-helper--require-roam ()
  "Load org-roam dailies, signalling a useful error if unavailable."
  (unless (require 'org-roam-dailies nil t)
    (error "org-roam is not available in this Emacs; check your init file")))

(defun org-cli-helper--split-template (entry)
  "Split org-roam capture template ENTRY into (HEAD BODY REST).

HEAD is the positional prefix up to but excluding the template body, and
REST is the trailing property list.  Note that org-roam's template syntax
is not `org-capture-templates' syntax: the body sits where org-capture
keeps its target, and the target travels in REST as `:target'.  The
patterns below deliberately mirror `org-roam-capture--convert-template',
which is what will ultimately parse whatever we hand back.

Returns nil for a two element prefix group or anything unrecognised."
  (pcase entry
    (`(,_key ,_desc) nil)
    (`(,key ,desc ,type ignore ,body . ,rest)
     (list (list key desc type 'ignore) body rest))
    (`(,key ,desc ,type (function ignore) ,body . ,rest)
     (list (list key desc type '(function ignore)) body rest))
    (`(,key ,desc ,type ,body . ,rest)
     (list (list key desc type) body rest))
    (_ nil)))

(defun org-cli-helper--describe-body (body)
  "Describe the template text BODY for JSON encoding."
  (cond
   ((stringp body) (list :body body :body_kind "string"))
   ((and (consp body) (eq (car body) 'file))
    (list :body (format "%s" (cadr body)) :body_kind "file"))
   ((and (consp body) (eq (car body) 'function))
    (list :body (format "%s" (cadr body)) :body_kind "function"))
   ((functionp body) (list :body (format "%s" body) :body_kind "function"))
   (t (list :body :null :body_kind "nil"))))

(defun org-cli-helper--describe-template (entry)
  "Describe capture template ENTRY as a plist for JSON encoding."
  (let ((split (org-cli-helper--split-template entry)))
    (if (null split)
        ;; A two element entry is a multi-key prefix group, not a template.
        (list :key (format "%s" (or (nth 0 entry) ""))
              :description (format "%s" (or (nth 1 entry) ""))
              :type :null
              :body :null
              :body_kind "nil"
              :group t)
      (let ((head (nth 0 split))
            (body (nth 1 split)))
        (append (list :key (format "%s" (or (nth 0 head) ""))
                      :description (format "%s" (or (nth 1 head) ""))
                      :type (format "%s" (or (nth 2 head) 'entry)))
                (org-cli-helper--describe-body body)
                (list :group :false))))))

(defun org-cli-helper-info ()
  "Report org-roam locations and dailies capture templates."
  (org-cli-helper--require-roam)
  (org-cli-helper--payload
   (list :roam_directory (expand-file-name org-roam-directory)
         :dailies_directory org-roam-dailies-directory
         :dailies_path (expand-file-name org-roam-dailies-directory
                                         org-roam-directory)
         :templates
         (vconcat (mapcar #'org-cli-helper--describe-template
                          org-roam-dailies-capture-templates)))))

(defun org-cli-helper--with-body (entry body)
  "Copy org-roam capture template ENTRY with its text replaced by BODY.

`:immediate-finish' is appended rather than prepended:
`org-roam-capture--convert-template' walks the property list forwards
building it up with `plist-put', so for duplicated keys the last one wins."
  (let ((split (org-cli-helper--split-template entry)))
    (unless split
      (error "Capture template %S has an unsupported shape" (car-safe entry)))
    (append (nth 0 split)
            (list body)
            (nth 2 split)
            (list :immediate-finish t))))

(defun org-cli-helper-commit (keys body)
  "Capture BODY into today's daily note using template KEYS.
BODY is a fully expanded capture template string."
  (org-cli-helper--require-roam)
  (let* ((org-roam-dailies-capture-templates
          (mapcar (lambda (entry)
                    (if (equal (car entry) keys)
                        (org-cli-helper--with-body entry body)
                      entry))
                  org-roam-dailies-capture-templates))
         (org-capture-last-stored-marker (make-marker)))
    (org-roam-dailies-capture-today nil keys)
    (let ((marker org-capture-last-stored-marker))
      (org-cli-helper--payload
       (list :file (if (and (markerp marker) (marker-buffer marker))
                       (or (buffer-file-name
                            (or (buffer-base-buffer (marker-buffer marker))
                                (marker-buffer marker)))
                           :null)
                     :null))))))

(defun org-cli-helper-capture-interactive (keys)
  "Run an interactive dailies capture with template KEYS.

Evaluated inside the frame `emacsclient -t' (or -c) just created, which
emacsclient keeps alive until it is deleted.  Finishing or aborting the
capture therefore has to delete that frame, otherwise the caller is left
sitting in Emacs with no obvious way back to the shell.

Only a frame emacsclient owns is ever deleted, so an unexpected
`selected-frame' cannot cost the daemon its own."
  (org-cli-helper--require-roam)
  (let ((frame (selected-frame))
        (hook nil))
    (setq hook
          (lambda ()
            (remove-hook 'org-capture-after-finalize-hook hook)
            (when (and (frame-live-p frame)
                       (frame-parameter frame 'client)
                       (cdr (frame-list)))
              (ignore-errors (delete-frame frame)))))
    (add-hook 'org-capture-after-finalize-hook hook)
    (condition-case err
        (if (and keys (not (equal keys "")))
            (org-roam-dailies-capture-today nil keys)
          (org-roam-dailies-capture-today))
      (error
       (remove-hook 'org-capture-after-finalize-hook hook)
       (signal (car err) (cdr err)))))
  nil)

(provide 'org-cli-helper-bootstrap)
;;; bootstrap.el ends here
