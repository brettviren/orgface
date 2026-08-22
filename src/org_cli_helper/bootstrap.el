;;; bootstrap.el --- daemon-side support for org-cli-helper  -*- lexical-binding: t; -*-

;; SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
;; SPDX-License-Identifier: GPL-3.0-or-later

;; Loaded into the managed `emacs --daemon' by org-cli-helper.  Every
;; function returns a base64-encoded UTF-8 JSON string so the CLI never has
;; to parse Emacs' printed representation of anything richer than a string.

;;; Code:

(require 'org-capture)

(defconst org-cli-helper-version "2"
  "Protocol version, probed by the CLI to detect a bootstrapped daemon.

Bumped when functions are added: a daemon started before the change has
the old definitions loaded and will not gain the new ones until it is
restarted.")

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

(defun org-cli-helper--require (feature)
  "Load FEATURE, signalling a useful error if it is unavailable."
  (unless (require feature nil t)
    (error "%s is not available in this Emacs; check your init file" feature)))

(defun org-cli-helper--require-roam ()
  "Load org-roam, signalling a useful error if unavailable."
  (org-cli-helper--require 'org-roam))

(defun org-cli-helper--require-dailies ()
  "Load org-roam dailies, signalling a useful error if unavailable."
  (org-cli-helper--require 'org-roam-dailies))

(defun org-cli-helper--stored-file (marker)
  "The file org-capture last stored into, per MARKER, or nil.

The capture happens in an indirect buffer, so the base buffer is the one
holding the file name once that indirect buffer has been killed."
  (when (and (markerp marker) (marker-buffer marker))
    (buffer-file-name (or (buffer-base-buffer (marker-buffer marker))
                          (marker-buffer marker)))))

(defun org-cli-helper--with-capture-frame (thunk)
  "Call THUNK, deleting this emacsclient frame once the capture finalises.

Evaluated inside the frame `emacsclient -t' (or -c) just created, which
emacsclient keeps alive until it is deleted.  Finishing or aborting the
capture therefore has to delete that frame, otherwise the caller is left
sitting in Emacs with no obvious way back to the shell.

Only a frame emacsclient owns is ever deleted, so an unexpected
`selected-frame' cannot cost the daemon its own."
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
        (funcall thunk)
      (error
       (remove-hook 'org-capture-after-finalize-hook hook)
       (signal (car err) (cdr err))))))

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
  (org-cli-helper--require-dailies)
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
  (org-cli-helper--require-dailies)
  (let* ((org-roam-dailies-capture-templates
          (mapcar (lambda (entry)
                    (if (equal (car entry) keys)
                        (org-cli-helper--with-body entry body)
                      entry))
                  org-roam-dailies-capture-templates))
         (org-capture-last-stored-marker (make-marker)))
    (org-roam-dailies-capture-today nil keys)
    (org-cli-helper--payload
     (list :file (or (org-cli-helper--stored-file org-capture-last-stored-marker)
                     :null)))))

(defun org-cli-helper-capture-interactive (keys)
  "Run an interactive dailies capture with template KEYS."
  (org-cli-helper--require-dailies)
  (org-cli-helper--with-capture-frame
   (lambda ()
     (if (and keys (not (equal keys "")))
         (org-roam-dailies-capture-today nil keys)
       (org-roam-dailies-capture-today))))
  nil)


;;; Topic notes
;;
;; Where `org commit' writes to today's daily note, `org note' writes to a
;; named org-roam topic.  Both are org-roam captures, so a note body is
;; expanded the same number of times as a commit body and needs no separate
;; escaping arithmetic on the CLI side.

(defconst org-cli-helper-note-target
  '(file+head "%<%Y%m%d%H%M%S>-${slug}.org" "#+title: ${title}\n")
  "Capture target for the topic notes `org-cli-helper-note' writes.

Only reached when the topic does not exist yet: for one that does,
`org-roam-capture--target-truepath' answers with the node's own file and
the head is never inserted.  The shape deliberately matches org-roam's own
default so a topic created here is indistinguishable from one created
interactively.")

(defun org-cli-helper--topic-node (topic)
  "An `org-roam-node' titled TOPIC, existing or waiting to be created.

`org-roam-capture-' gives a node with no ID an ID, and its target creates
the file, so a fresh node is all that is needed to make a new topic."
  (or (org-roam-node-from-title-or-alias topic)
      (org-roam-node-create :title topic)))

(defun org-cli-helper--note-capture (topic body immediate)
  "Capture BODY as an entry in the org-roam node titled TOPIC.

BODY is a fully expanded capture template string.  With IMMEDIATE the
capture finalises without anyone at a keyboard.  Returns the node captured
into.

The entry lands at the end of the topic's file: the target leaves point at
the start of the file, which is not a heading, and org-capture appends a
top level entry when its target is not an entry."
  (let ((node (org-cli-helper--topic-node topic))
        (template (list "n" "context note" 'entry body
                        :target org-cli-helper-note-target)))
    (org-roam-capture-
     :node node
     :keys "n"
     :templates (list (if immediate
                          (append template '(:immediate-finish t))
                        template)))
    node))

(defun org-cli-helper-note (topic body)
  "Capture BODY into the org-roam topic TOPIC, reporting where it landed."
  (org-cli-helper--require-roam)
  (let* ((org-capture-last-stored-marker (make-marker))
         (node (org-cli-helper--note-capture topic body t)))
    (org-cli-helper--payload
     (list :topic topic
           :id (or (org-roam-node-id node) :null)
           :file (or (org-cli-helper--stored-file org-capture-last-stored-marker)
                     (org-roam-node-file node)
                     :null)))))

(defun org-cli-helper-note-interactive (topic body)
  "Run an interactive capture of BODY into the org-roam topic TOPIC."
  (org-cli-helper--require-roam)
  (org-cli-helper--with-capture-frame
   (lambda () (org-cli-helper--note-capture topic body nil)))
  nil)

(defun org-cli-helper-topic (topic)
  "Report the org-roam node titled TOPIC, with nulls if there is none."
  (org-cli-helper--require-roam)
  (let ((node (org-roam-node-from-title-or-alias topic)))
    (org-cli-helper--payload
     (list :topic topic
           :id (or (and node (org-roam-node-id node)) :null)
           :file (or (and node (org-roam-node-file node)) :null)))))

;;; The org store
;;
;; Reading org through Emacs rather than through a standalone parser is
;; worth the round trip for one reason: org-element is the definition of
;; what org means, and it is reading the person's own configuration --
;; their link types, their TODO keywords, their in-buffer settings.  No
;; independent parser can promise that.

(defun org-cli-helper--raw-payload (json)
  "Encode JSON, already a JSON string, for the CLI to decode.

Distinct from `org-cli-helper--payload', which serialises an Elisp
object; passing an exported document through that would encode the JSON
as a JSON string rather than sending it as itself."
  (base64-encode-string (encode-coding-string json 'utf-8) t))

(defun org-cli-helper--visiting (path)
  "The live buffer visiting PATH, or nil."
  (find-buffer-visiting (expand-file-name path)))

(defun org-cli-helper-export-json (path)
  "Export the org file at PATH as ox-json JSON.

A buffer already visiting PATH is used in preference to re-reading the
file, so what comes back is what Emacs currently has -- including edits
not yet saved.  That is the point of asking Emacs at all; a caller
wanting the file as stored should read the file.

A buffer opened here is killed again, so that repeated exports do not
accumulate buffers in a long-lived daemon."
  (org-cli-helper--require 'ox-json)
  (let* ((file (expand-file-name path))
         (existing (org-cli-helper--visiting file))
         (buffer (or existing (find-file-noselect file t))))
    (unwind-protect
        (with-current-buffer buffer
          (let ((org-export-with-broken-links t))
            (org-cli-helper--raw-payload
             ;; Deterministic refs, so that exporting the same document
             ;; twice compares equal.  The CLI recomputes its own refs
             ;; regardless, but a payload that changed every call would
             ;; defeat any caching or diffing on the way.
             (org-export-as 'json nil nil nil
                            (list :json-deterministic-refs t)))))
      (unless existing (kill-buffer buffer)))))

(defun org-cli-helper-buffer-state (path)
  "Report whether Emacs holds PATH, and whether it has unsaved changes.

Asked before writing a file, so that unsaved work is never silently
overwritten by a store that is only one of several writers."
  (let* ((file (expand-file-name path))
         (buffer (org-cli-helper--visiting file)))
    (org-cli-helper--payload
     (list :path file
           :visiting (if buffer t :false)
           :modified (if (and buffer (buffer-modified-p buffer)) t :false)))))

(defun org-cli-helper-revert (path)
  "Reload PATH into its buffer, if Emacs has one.

Called after writing the file from outside, so Emacs is not left showing
content that is no longer what is on disk."
  (let* ((file (expand-file-name path))
         (buffer (org-cli-helper--visiting file)))
    (when buffer
      (with-current-buffer buffer
        (revert-buffer :ignore-auto :noconfirm :preserve-modes)))
    (org-cli-helper--payload
     (list :path file :reverted (if buffer t :false)))))

(provide 'org-cli-helper-bootstrap)
;;; bootstrap.el ends here
