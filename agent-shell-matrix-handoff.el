;;; agent-shell-matrix-handoff.el --- Matrix handoff for agent-shell -*- lexical-binding: t; -*-

;; Author: Copilot
;; Version: 1.0
;; Package-Requires: ((emacs "27.1") (agent-shell "1.0"))
;; URL: https://github.com/edd/matrix-proxy-bot

;;; Commentary:
;;
;; Enable seamless handoff of agent-shell sessions to Matrix chat.
;; 
;; Commands:
;;   M-x agent-shell-matrix-handoff  — Hand off current session to Matrix
;;   M-x agent-shell-matrix-return   — Return session from Matrix to Emacs
;;   M-x agent-shell-matrix-webhook-start — Start webhook server
;;   M-x agent-shell-matrix-webhook-stop  — Stop webhook server
;;
;; The bot (matrix-proxy-bot) runs on localhost:8765 and relays messages
;; between the agent-shell session and a Matrix room.

;;; Code:

(require 'json)
(require 'map)
(require 'seq)
(require 'ansi-color)
(require 'agent-shell)

(declare-function acp-send-request "acp")
(declare-function acp-make-session-set-mode-request "acp")
(declare-function acp-make-session-set-model-request "acp")
(declare-function agent-shell--state "agent-shell")
(declare-function agent-shell--get-available-modes "agent-shell")
(declare-function agent-shell-subscribe-to "agent-shell")
(declare-function agent-shell-unsubscribe "agent-shell")
(defvar agent-shell-idle-timeout)

(defvar agent-shell-matrix-handoff--state nil
  "State during Matrix handoff session.
Contains: ((room_id . ID) (session_id . ID) (shell_buffer . BUFFER))")

(defgroup agent-shell-matrix nil
  "Matrix handoff for agent-shell."
  :group 'agent-shell
  :prefix "agent-shell-matrix-")

(defcustom agent-shell-matrix-handoff-context-exchanges 2
  "Number of prompt/response exchanges to include in Matrix context.
Set to 0 to disable context replay."
  :type 'integer
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-webhook-secret nil
  "Bearer token for authenticating with matrix-proxy-bot.
Must be set before use."
  :type '(choice (const :tag "Not set" nil) string)
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-bot-url "http://127.0.0.1:8765"
  "URL of the matrix-proxy-bot server."
  :type 'string
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-webhook-port 9999
  "Port for the webhook server."
  :type 'integer
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-webhook-host "0.0.0.0"
  "Host for the webhook server to bind to."
  :type 'string
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-webhook-url nil
  "URL the bot should use to reach the webhook server.
If nil, defaults to http://<webhook-host>:<webhook-port>/webhook."
  :type '(choice (const :tag "Auto-detect" nil) string)
  :group 'agent-shell-matrix)

(defcustom agent-shell-matrix-handoff-idle-timeout
  '((permission-request . 15)
    (turn-complete . 60))
  "Per-event idle timeouts in seconds during Matrix handoff.

When handoff is active, this value is applied buffer-locally to
`agent-shell-idle-timeout' so the agent emits an `idle' event after the
specified seconds of inactivity following each named event.  The handoff
relays those idle events to the Matrix room as a nudge that the agent is
waiting for input.

See `agent-shell-idle-timeout' for the supported format (number or alist)."
  :type '(choice (number :tag "Single timeout (seconds)")
                 (alist :key-type symbol :value-type integer))
  :group 'agent-shell-matrix)

(defvar agent-shell-matrix-webhook-server-process nil
  "Server process listening for webhook calls.")

(defvar agent-shell-matrix-webhook-connections nil
  "Hash table of active connections: process -> buffer.")

(defvar agent-shell-matrix-handoff--output-buffer ""
  "Accumulates agent output during handoff for relay to Matrix.")

(defvar agent-shell-matrix-handoff--relay-timer nil
  "Timer to debounce relay of accumulated output to Matrix.")

(defvar agent-shell-matrix-handoff--typing nil
  "Non-nil when typing indicator is active in Matrix room.")

(defun agent-shell-matrix-handoff--capture-context (buffer)
  "Capture recent prompt/response exchanges from BUFFER for Matrix context.
Extracts agent responses (markdown) and user prompts, skipping tool calls
and collapsed sections."
  (if (<= agent-shell-matrix-handoff-context-exchanges 0)
      ""
    (with-current-buffer buffer
      (let (entries (pos (point-min)))
        (while (< pos (point-max))
          (let* ((section (get-text-property pos 'agent-shell-ui-section))
                 (state (get-text-property pos 'agent-shell-ui-state))
                 (qid (and state (cdr (assoc :qualified-id state))))
                 (next (or (next-single-property-change
                            pos 'agent-shell-ui-section nil (point-max))
                           (point-max))))
            (cond
             ;; Agent response body
             ((and (eq section 'body) qid
                   (string-match "agent_message_chunk" qid))
              (let ((text (string-trim (buffer-substring-no-properties pos next))))
                (when (> (length text) 0)
                  (push (cons "response" text) entries))))
             ;; User prompt (unsectioned text with prompt marker)
             ((null section)
              (let ((text (buffer-substring-no-properties pos next)))
                (when (string-match " ❯ \\([^ \n].+\\)" text)
                  (let ((prompt (match-string 1 text)))
                    (setq prompt (replace-regexp-in-string
                                  "<shell-maker-end-of-prompt>.*" "" prompt t t))
                    (setq prompt (string-trim prompt))
                    (when (> (length prompt) 0)
                      (push (cons "prompt" prompt) entries)))))))
            (setq pos next)))
        (let* ((all (reverse entries))
               (tail (last all (* 2 agent-shell-matrix-handoff-context-exchanges))))
          (if tail
              (mapconcat
               (lambda (pair)
                 (if (string= (car pair) "prompt")
                     (format "`> %s`" (cdr pair))
                   (cdr pair)))
               tail "\n\n")
            ""))))))

(defun agent-shell-matrix-handoff--ensure-secret ()
  "Error if `agent-shell-matrix-webhook-secret' is not configured."
  (unless agent-shell-matrix-webhook-secret
    (error "Set `agent-shell-matrix-webhook-secret' before using Matrix handoff")))

(defun agent-shell-matrix-handoff--call-bot (endpoint method data)
  "Call matrix-proxy-bot ENDPOINT with METHOD and DATA.
Returns parsed JSON response."
  (agent-shell-matrix-handoff--ensure-secret)
  (let* ((url (concat agent-shell-matrix-bot-url endpoint))
         (json-data (and data (json-encode data)))
         (args (list "-s" "-X" method
                     "-H" (format "Authorization: Bearer %s"
                                  agent-shell-matrix-webhook-secret)
                     "-H" "Content-Type: application/json; charset=utf-8")))
    (when json-data
      (setq args (append args (list "-d" json-data))))
    (setq args (append args (list url)))
    (with-temp-buffer
      (apply #'call-process "curl" nil t nil args)
      (goto-char (point-min))
      (condition-case err
          (json-parse-buffer :object-type 'alist :array-type 'list)
        (error
         (message "JSON parse error: %s" err)
         nil)))))

(defun agent-shell-matrix-webhook--parse-http-body (buffer-str)
  "Extract JSON body from HTTP request string BUFFER-STR.
Validates Content-Length before parsing to avoid truncated payloads."
  (let ((header-end (string-match "\r\n\r\n" buffer-str)))
    (when header-end
      (let* ((headers (substring buffer-str 0 header-end))
             (body (substring buffer-str (+ header-end 4)))
             (content-length
              (when (string-match "Content-Length: *\\([0-9]+\\)" headers)
                (string-to-number (match-string 1 headers)))))
        (when (or (null content-length)
                  (>= (length body) content-length))
          (unless (string-empty-p (string-trim body))
            (ignore-errors
              (json-parse-string body :object-type 'alist :array-type 'list))))))))

(defun agent-shell-matrix-webhook--process-message (payload)
  "Process incoming webhook message from matrix-proxy-bot."
  (let ((action (alist-get 'action payload))
        (value (alist-get 'value payload))
        (msg (alist-get 'message payload))
        (shell-buffer (and agent-shell-matrix-handoff--state
                          (cdr (assoc "shell_buffer" agent-shell-matrix-handoff--state)))))
    
    (cond
     ;; Command: handoff_end
     ((and action (string= action "handoff_end"))
      (agent-shell-matrix-handoff--cleanup)
      (message "✓ Session returned to Emacs"))

     ;; Command: set_mode
     ((and action (string= action "set_mode") value shell-buffer)
      (with-current-buffer shell-buffer
        (agent-shell-matrix-handoff--set-mode value))
      (message "✓ Mode → %s" value))

     ;; Command: set_model
     ((and action (string= action "set_model") value shell-buffer)
      (with-current-buffer shell-buffer
        (agent-shell-matrix-handoff--set-model value))
      (message "✓ Model → %s" value))
     
     ;; Regular message from user in Matrix - submit to agent via shell-maker
     (msg
      (when shell-buffer
        (let ((room-id (cdr (assoc "room_id" agent-shell-matrix-handoff--state))))
          (when room-id
            (agent-shell-matrix-handoff--set-typing room-id t)
            (setq agent-shell-matrix-handoff--typing t)))
        (with-current-buffer shell-buffer
          (shell-maker-submit :input msg)))))))

(defun agent-shell-matrix-handoff--set-mode (mode-name)
  "Set agent-shell session mode to MODE-NAME programmatically."
  (let* ((state (agent-shell--state))
         (modes (agent-shell--get-available-modes state))
         (mode (seq-find (lambda (m)
                           (string-equal-ignore-case (map-elt m :name) mode-name))
                         modes))
         (mode-id (and mode (map-elt mode :id))))
    (unless mode-id
      (error "Unknown mode: %s" mode-name))
    (acp-send-request
     :client (map-elt state :client)
     :request (acp-make-session-set-mode-request
               :session-id (map-nested-elt state '(:session :id))
               :mode-id mode-id)
     :buffer (current-buffer)
     :on-success (lambda (_response)
                   (let ((session (map-elt (agent-shell--state) :session)))
                     (map-put! session :mode-id mode-id)
                     (map-put! (agent-shell--state) :session session))))))

(defun agent-shell-matrix-handoff--set-model (model-name)
  "Set agent-shell session model to MODEL-NAME programmatically."
  (let* ((state (agent-shell--state))
         (models (map-nested-elt state '(:session :models)))
         (model (seq-find (lambda (m)
                            (or (string-equal-ignore-case (map-elt m :name) model-name)
                                (string-equal-ignore-case (map-elt m :model-id) model-name)))
                          models))
         (model-id (and model (map-elt model :model-id))))
    (unless model-id
      (error "Unknown model: %s" model-name))
    (acp-send-request
     :client (map-elt state :client)
     :request (acp-make-session-set-model-request
               :session-id (map-nested-elt state '(:session :id))
               :model-id model-id)
     :on-success (lambda (_response)
                   (let ((session (map-elt (agent-shell--state) :session)))
                     (map-put! session :model-id model-id)
                     (map-put! (agent-shell--state) :session session))))))

(defun agent-shell-matrix-handoff--get-capabilities ()
  "Return an alist of available modes/models and current selections."
  (let* ((state (agent-shell--state))
         (modes (agent-shell--get-available-modes state))
         (models (map-nested-elt state '(:session :models)))
         (current-mode-id (map-nested-elt state '(:session :mode-id)))
         (current-model-id (map-nested-elt state '(:session :model-id)))
         result)
    (when modes
      (push (cons "available_modes"
                   (mapcar (lambda (m) (map-elt m :name)) modes))
            result)
      (when current-mode-id
        (let ((mode (seq-find (lambda (m) (string= (map-elt m :id) current-mode-id)) modes)))
          (when mode
            (push (cons "current_mode" (map-elt mode :name)) result)))))
    (when models
      (push (cons "available_models"
                   (mapcar (lambda (m) (map-elt m :model-id)) models))
            result)
      (when current-model-id
        (push (cons "current_model" current-model-id) result)))
    result))

(defun agent-shell-matrix-handoff--relay-async (room-id session-id text)
  "Asynchronously relay TEXT to Matrix room via bot webhook."
  (let* ((url (concat agent-shell-matrix-bot-url "/webhook/message"))
         (json-data (json-encode (list (cons "room_id" room-id)
                                       (cons "session_id" session-id)
                                       (cons "response_text" text)))))
    (start-process "matrix-relay" nil "curl" "-s"
                   "-X" "POST"
                   "-H" (format "Authorization: Bearer %s"
                                agent-shell-matrix-webhook-secret)
                   "-H" "Content-Type: application/json; charset=utf-8"
                   "-d" json-data
                   url)))

(defun agent-shell-matrix-handoff--set-typing (room-id typing)
  "Asynchronously set typing indicator for ROOM-ID."
  (let* ((url (concat agent-shell-matrix-bot-url "/typing"))
         (json-data (json-encode (list (cons "room_id" room-id)
                                       (cons "typing" (if typing t :json-false))))))
    (start-process "matrix-typing" nil "curl" "-s"
                   "-X" "POST"
                   "-H" (format "Authorization: Bearer %s"
                                agent-shell-matrix-webhook-secret)
                   "-H" "Content-Type: application/json; charset=utf-8"
                   "-d" json-data
                   url)))

(defun agent-shell-matrix-handoff--extract-notification (args)
  "Extract the notification payload from advised agent-shell ARGS."
  (cond
   ((and (listp args) (plist-member args :acp-notification))
    (plist-get args :acp-notification))
   ((and (listp args) (plist-member args :notification))
    (plist-get args :notification))
   ((and (= (length args) 1) (listp (car args)))
    (car args))
   (t nil)))

(defun agent-shell-matrix-handoff--flush-output ()
  "Send accumulated agent output to Matrix room and stop typing."
  (when (and agent-shell-matrix-handoff--state
             (not (string-empty-p agent-shell-matrix-handoff--output-buffer)))
    (let* ((room-id (cdr (assoc "room_id" agent-shell-matrix-handoff--state)))
           (session-id (cdr (assoc "session_id" agent-shell-matrix-handoff--state)))
           (text (string-trim (ansi-color-filter-apply
                               agent-shell-matrix-handoff--output-buffer))))
      (setq agent-shell-matrix-handoff--output-buffer "")
      (setq agent-shell-matrix-handoff--relay-timer nil)
      (when (and room-id (not (string-empty-p text)))
        (agent-shell-matrix-handoff--relay-async room-id session-id text))
      ;; Stop typing indicator
      (when (and room-id agent-shell-matrix-handoff--typing)
        (agent-shell-matrix-handoff--set-typing room-id nil)
        (setq agent-shell-matrix-handoff--typing nil)))))

(defun agent-shell-matrix-handoff--notification-advice (orig-fun &rest args)
  "Advice around `agent-shell--on-notification' to relay streaming text.

Streams `agent_message_chunk' content to Matrix during handoff.  Tool
calls, turn completion, and idle nudges are handled separately via
`agent-shell-subscribe-to' (see
`agent-shell-matrix-handoff--install-subscriptions') since those are
exposed as first-class events upstream.  Streaming text chunks are not
yet exposed as events, so this advice remains for that one case."
  (apply orig-fun args)
  (when agent-shell-matrix-handoff--state
    (let* ((notification (agent-shell-matrix-handoff--extract-notification args))
           (update (map-elt (map-elt notification 'params) 'update))
           (session-update (and update (map-elt update 'sessionUpdate))))
      (when (equal session-update "agent_message_chunk")
        (let ((text (map-nested-elt update '(content text))))
          (when text
            (setq agent-shell-matrix-handoff--output-buffer
                  (concat agent-shell-matrix-handoff--output-buffer text))
            (when agent-shell-matrix-handoff--relay-timer
              (cancel-timer agent-shell-matrix-handoff--relay-timer))
            (setq agent-shell-matrix-handoff--relay-timer
                  (run-at-time 2.0 nil #'agent-shell-matrix-handoff--flush-output))))))))

;;; Event subscriptions (idle / tool-call / turn-complete)
;;
;; Upstream agent-shell exposes a pub/sub event system via
;; `agent-shell-subscribe-to'.  We use it for everything that has a
;; first-class event:
;;
;;   tool-call-update -> relay "🔧 title" to Matrix
;;   turn-complete    -> flush any pending streamed text
;;   idle             -> nudge Matrix that the agent is waiting on the user
;;
;; Subscription tokens live in `agent-shell-matrix-handoff--state' so that
;; `agent-shell-matrix-return' can unsubscribe symmetrically.

(defun agent-shell-matrix-handoff--state-room-id ()
  "Return current handoff room id, or nil."
  (cdr (assoc "room_id" agent-shell-matrix-handoff--state)))

(defun agent-shell-matrix-handoff--state-session-id ()
  "Return current handoff session id, or nil."
  (cdr (assoc "session_id" agent-shell-matrix-handoff--state)))

(defun agent-shell-matrix-handoff--on-tool-call-update (event)
  "Relay tool-call updates from EVENT to the Matrix room.
Deduplicates by `:tool-call-id' so a single tool only emits one Matrix
line per turn, regardless of how many `tool-call-update' events fire."
  (when agent-shell-matrix-handoff--state
    (let* ((data (map-elt event :data))
           (tool-call-id (map-elt data :tool-call-id))
           (tool-call (map-elt data :tool-call))
           (title (map-elt tool-call :title))
           (room-id (agent-shell-matrix-handoff--state-room-id))
           (session-id (agent-shell-matrix-handoff--state-session-id))
           (announced (cdr (assoc "announced_tool_calls"
                                  agent-shell-matrix-handoff--state))))
      (when (and title room-id tool-call-id
                 (not (member tool-call-id announced)))
        (setf (alist-get "announced_tool_calls"
                         agent-shell-matrix-handoff--state nil nil #'equal)
              (cons tool-call-id announced))
        (agent-shell-matrix-handoff--flush-output)
        (agent-shell-matrix-handoff--relay-async
         room-id session-id (format "🔧 %s" title))))))

(defun agent-shell-matrix-handoff--on-turn-complete (_event)
  "Flush pending text and reset per-turn dedup state."
  (when agent-shell-matrix-handoff--state
    (agent-shell-matrix-handoff--flush-output)
    (setf (alist-get "announced_tool_calls"
                     agent-shell-matrix-handoff--state nil nil #'equal)
          nil)))

(defun agent-shell-matrix-handoff--idle-message (idle-event)
  "Format the Matrix nudge string for IDLE-EVENT."
  (pcase idle-event
    ('permission-request "⏰ Agent is waiting for a permission decision")
    ('turn-complete      "💤 Agent is idle — awaiting your next prompt")
    (_ (format "⏰ Agent idle (after %s)" idle-event))))

(defun agent-shell-matrix-handoff--on-idle (event)
  "Relay an idle nudge from EVENT to the Matrix room."
  (when agent-shell-matrix-handoff--state
    (let* ((data (map-elt event :data))
           (idle-event (map-elt data :idle-event))
           (room-id (agent-shell-matrix-handoff--state-room-id))
           (session-id (agent-shell-matrix-handoff--state-session-id)))
      (when room-id
        (agent-shell-matrix-handoff--flush-output)
        (agent-shell-matrix-handoff--relay-async
         room-id session-id
         (agent-shell-matrix-handoff--idle-message idle-event))))))

(defun agent-shell-matrix-handoff--on-clean-up (_event)
  "Tear down handoff if the underlying agent-shell buffer dies."
  (when agent-shell-matrix-handoff--state
    (agent-shell-matrix-handoff--cleanup)
    (message "Matrix handoff ended (agent-shell buffer was killed)")))

(defun agent-shell-matrix-handoff--install-subscriptions (shell-buffer)
  "Subscribe to agent-shell pub/sub events on SHELL-BUFFER.
Returns a list of subscription tokens for later cleanup."
  (list (agent-shell-subscribe-to
         :shell-buffer shell-buffer
         :event 'tool-call-update
         :on-event #'agent-shell-matrix-handoff--on-tool-call-update)
        (agent-shell-subscribe-to
         :shell-buffer shell-buffer
         :event 'turn-complete
         :on-event #'agent-shell-matrix-handoff--on-turn-complete)
        (agent-shell-subscribe-to
         :shell-buffer shell-buffer
         :event 'idle
         :on-event #'agent-shell-matrix-handoff--on-idle)
        (agent-shell-subscribe-to
         :shell-buffer shell-buffer
         :event 'clean-up
         :on-event #'agent-shell-matrix-handoff--on-clean-up)))

(defun agent-shell-matrix-handoff--remove-subscriptions (shell-buffer tokens)
  "Unsubscribe TOKENS from SHELL-BUFFER (no-op if buffer is dead)."
  (when (buffer-live-p shell-buffer)
    (with-current-buffer shell-buffer
      (dolist (tok tokens)
        (ignore-errors
          (agent-shell-unsubscribe :subscription tok))))))

(defun agent-shell-matrix-handoff--cleanup ()
  "Unwind all handoff side effects.
Idempotent: safe to call from `agent-shell-matrix-return',
the webhook `handoff_end' branch, or the upstream `clean-up' event."
  (let ((shell-buffer (cdr (assoc "shell_buffer" agent-shell-matrix-handoff--state)))
        (tokens (cdr (assoc "subscriptions" agent-shell-matrix-handoff--state))))
    (when tokens
      (agent-shell-matrix-handoff--remove-subscriptions shell-buffer tokens))
    (when (buffer-live-p shell-buffer)
      (with-current-buffer shell-buffer
        (kill-local-variable 'agent-shell-idle-timeout)))
    (when agent-shell-matrix-handoff--relay-timer
      (cancel-timer agent-shell-matrix-handoff--relay-timer)
      (setq agent-shell-matrix-handoff--relay-timer nil))
    (setq agent-shell-matrix-handoff--output-buffer "")
    (setq agent-shell-matrix-handoff--typing nil)
    (setq agent-shell-matrix-handoff--state nil)
    (advice-remove 'agent-shell--on-notification
                   #'agent-shell-matrix-handoff--notification-advice)))

(defun agent-shell-matrix-handoff--check-upstream-api ()
  "Error if the running `agent-shell' lacks the pub/sub idle-events API."
  (unless (and (fboundp 'agent-shell-subscribe-to)
               (fboundp 'agent-shell-unsubscribe)
               (boundp 'agent-shell-idle-timeout))
    (user-error
     "Matrix handoff requires a newer agent-shell with pub/sub idle events")))

(defun agent-shell-matrix-webhook--send-response (process status-code body)
  "Send HTTP response to PROCESS with STATUS-CODE and JSON BODY."
  (let ((response (format "HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                          status-code
                          (length body)
                          body)))
    (process-send-string process response)
    (delete-process process)))

(defun agent-shell-matrix-webhook--check-auth (buffer-str)
  "Validate Authorization header in HTTP request BUFFER-STR."
  (let ((header-end (or (string-match "\r\n\r\n" buffer-str) (length buffer-str))))
    (when (string-match "Authorization: *Bearer +\\(\\S-+\\)" (substring buffer-str 0 header-end))
      (string= (match-string 1 (substring buffer-str 0 header-end))
               agent-shell-matrix-webhook-secret))))

(defun agent-shell-matrix-webhook--client-filter (process data)
  "Filter for webhook client connections."
  (let ((buffer (gethash process agent-shell-matrix-webhook-connections)))
    (unless buffer
      (setq buffer (generate-new-buffer " *webhook-client*"))
      (puthash process buffer agent-shell-matrix-webhook-connections))
    (with-current-buffer buffer
      (insert data)
      (when (string-match "\r\n\r\n" (buffer-string))
        (let ((request (buffer-string)))
          (cond
           ((not (agent-shell-matrix-webhook--check-auth request))
            (agent-shell-matrix-webhook--send-response process 401 "{\"error\":\"Unauthorized\"}"))
           (t
            (let ((payload (agent-shell-matrix-webhook--parse-http-body request)))
              (if payload
                  (progn
                    (agent-shell-matrix-webhook--process-message payload)
                    (agent-shell-matrix-webhook--send-response process 200 "{\"status\":\"ok\"}"))
                (agent-shell-matrix-webhook--send-response process 400 "{\"error\":\"Invalid JSON\"}"))))))
        (remhash process agent-shell-matrix-webhook-connections)
        (kill-buffer buffer)))))

;;;###autoload
(defun agent-shell-matrix-webhook-start ()
  "Start the webhook server listening for matrix-proxy-bot calls."
  (interactive)
  (agent-shell-matrix-handoff--ensure-secret)
  (unless agent-shell-matrix-webhook-connections
    (setq agent-shell-matrix-webhook-connections (make-hash-table)))
  
  (if agent-shell-matrix-webhook-server-process
      (message "Webhook server already running on :%d" agent-shell-matrix-webhook-port)
    (setq agent-shell-matrix-webhook-server-process
          (make-network-process
           :name "webhook-server"
           :service agent-shell-matrix-webhook-port
           :server t
           :host agent-shell-matrix-webhook-host
           :filter #'agent-shell-matrix-webhook--client-filter
           :noquery t))
    (message "✓ Webhook server started on :%d" agent-shell-matrix-webhook-port)))

;;;###autoload
(defun agent-shell-matrix-webhook-stop ()
  "Stop the webhook server."
  (interactive)
  (when agent-shell-matrix-webhook-server-process
    (delete-process agent-shell-matrix-webhook-server-process)
    (setq agent-shell-matrix-webhook-server-process nil)
    (when agent-shell-matrix-webhook-connections
      (maphash (lambda (_proc buf)
                 (kill-buffer buf))
               agent-shell-matrix-webhook-connections)
      (clrhash agent-shell-matrix-webhook-connections))
    (message "✓ Webhook server stopped")))

;;;###autoload
(defun agent-shell-matrix-handoff ()
  "Initiate handoff from Emacs agent-shell to Matrix.

Creates a Matrix room and enables bidirectional message relay.
Use M-x agent-shell-matrix-return to bring the session back."
  (interactive)
  (unless (derived-mode-p 'agent-shell-mode)
    (error "Not in agent-shell buffer"))
  (when agent-shell-matrix-handoff--state
    (user-error "A Matrix handoff is already active; M-x agent-shell-matrix-return first"))
  (agent-shell-matrix-handoff--check-upstream-api)

  ;; Start webhook server if not running
  (unless agent-shell-matrix-webhook-server-process
    (agent-shell-matrix-webhook-start))
  
  (let* ((state (agent-shell--state))
         (session-state (map-elt state :session))
         (session-id (alist-get :id session-state))
         (hostname (system-name))
         (context (agent-shell-matrix-handoff--capture-context (current-buffer)))
         (handoff-data (list (cons "session_id" session-id)
                             (cons "hostname" hostname)
                             (cons "webhook_url" (or agent-shell-matrix-webhook-url
                                                     (format "http://%s:%d/webhook"
                                                             agent-shell-matrix-webhook-host
                                                             agent-shell-matrix-webhook-port)))
                             (cons "webhook_secret" agent-shell-matrix-webhook-secret)))
         (capabilities (agent-shell-matrix-handoff--get-capabilities))
         (handoff-data (append handoff-data capabilities))
         (handoff-data (if (and context (not (string-empty-p context)))
                          (append handoff-data (list (cons "message" context)))
                          handoff-data))
         (response (agent-shell-matrix-handoff--call-bot
                    "/handoff"
                    "POST"
                    handoff-data))
         (room-id (alist-get 'room_id response)))
    
    (unless room-id
      (error "Failed to create handoff room"))
    
    (setq agent-shell-matrix-handoff--state
          (list (cons "room_id" room-id)
                (cons "session_id" session-id)
                (cons "shell_buffer" (current-buffer))))

    ;; Apply per-event idle timeouts buffer-locally so the agent emits
    ;; `idle' events on a schedule appropriate for handoff (short for
    ;; permission requests, longer for turn-complete).
    (setq-local agent-shell-idle-timeout
                agent-shell-matrix-handoff-idle-timeout)

    ;; Subscribe to first-class events (tool calls, turn end, idle).
    ;; Tokens go in the handoff state so `agent-shell-matrix-return' can
    ;; tear them down symmetrically.
    (let ((tokens (agent-shell-matrix-handoff--install-subscriptions
                   (current-buffer))))
      (push (cons "subscriptions" tokens)
            agent-shell-matrix-handoff--state))

    ;; Streaming text chunks aren't exposed as pub/sub events yet, so we
    ;; still need a narrow advice for `agent_message_chunk' relay.
    (advice-add 'agent-shell--on-notification :around
                #'agent-shell-matrix-handoff--notification-advice)

    (message "✓ Handed off to Matrix: %s" room-id)))

;;;###autoload
(defun agent-shell-matrix-return ()
  "Return from Matrix back to Emacs.

Notifies the bot to return ownership to Emacs and ends the handoff."
  (interactive)
  (unless agent-shell-matrix-handoff--state
    (error "No active handoff session"))
  
  (let ((room-id (cdr (assoc "room_id" agent-shell-matrix-handoff--state)))
        (session-id (cdr (assoc "session_id" agent-shell-matrix-handoff--state))))

    (agent-shell-matrix-handoff--call-bot
     "/webhook/message"
     "POST"
     (list (cons "room_id" room-id)
           (cons "session_id" session-id)
           (cons "action" "handoff_end")))

    (agent-shell-matrix-handoff--cleanup)
    (message "✓ Session returned to Emacs")))

;;;###autoload
(defun agent-shell-matrix-toggle ()
  "Toggle handoff between Emacs and Matrix.
If no active handoff, initiates one. If active, returns to Emacs."
  (interactive)
  (if agent-shell-matrix-handoff--state
      (agent-shell-matrix-return)
    (agent-shell-matrix-handoff)))

(with-eval-after-load 'agent-shell
  (define-key agent-shell-mode-map (kbd "C-c H") #'agent-shell-matrix-toggle))

(provide 'agent-shell-matrix-handoff)

;;; agent-shell-matrix-handoff.el ends here
