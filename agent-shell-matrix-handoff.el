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
(require 'url)
(require 'agent-shell)

(defvar agent-shell-matrix-handoff--state nil
  "State during Matrix handoff session.
Contains: ((room_id . ID) (session_id . ID) (shell_buffer . BUFFER))")

(defvar agent-shell-matrix-handoff-context-exchanges 2
  "Number of prompt/response exchanges to include in Matrix context.
Set to 0 to disable context replay.")

(defvar agent-shell-matrix-webhook-secret "REDACTED-SET-VIA-CUSTOMIZE"
  "Bearer token for authenticating with matrix-proxy-bot.")

(defvar agent-shell-matrix-bot-url "http://127.0.0.1:8765"
  "URL of the matrix-proxy-bot server.")

(defvar agent-shell-matrix-webhook-port 9999
  "Port for the webhook server.")

(defvar agent-shell-matrix-webhook-host "127.0.0.1"
  "Host for the webhook server to bind to.")

(defvar agent-shell-matrix-webhook-url nil
  "URL the bot should use to reach the webhook server.
If nil, defaults to http://<webhook-host>:<webhook-port>/webhook.")

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

(defun agent-shell-matrix-handoff--call-bot (endpoint method data)
  "Call matrix-proxy-bot ENDPOINT with METHOD and DATA.
Returns parsed JSON response."
  (let* ((url-request-method method)
         (url-request-extra-headers
          (list (cons "Authorization" (format "Bearer %s" agent-shell-matrix-webhook-secret))
                (cons "Content-Type" "application/json; charset=utf-8")))
         (url-request-data (and data (encode-coding-string (json-encode data) 'utf-8))))
    (with-current-buffer (url-retrieve-synchronously
                          (concat agent-shell-matrix-bot-url endpoint))
      (goto-char (point-min))
      (re-search-forward "^$")
      (condition-case err
          (json-parse-buffer :object-type 'alist :array-type 'list)
        (error
         (message "JSON parse error: %s" err)
         nil)))))

(defun agent-shell-matrix-webhook--parse-http-body (buffer-str)
  "Extract JSON body from HTTP request string BUFFER-STR."
  (let ((parts (split-string buffer-str "\r\n\r\n")))
    (when (> (length parts) 1)
      (let ((body (nth 1 parts)))
        (unless (string-empty-p (string-trim body))
          (ignore-errors
            (json-parse-string body :object-type 'alist :array-type 'list)))))))

(defun agent-shell-matrix-webhook--process-message (payload)
  "Process incoming webhook message from matrix-proxy-bot."
  (let ((action (alist-get 'action payload))
        (msg (alist-get 'message payload))
        (shell-buffer (and agent-shell-matrix-handoff--state
                          (cdr (assoc "shell_buffer" agent-shell-matrix-handoff--state)))))
    
    (cond
     ;; Command: handoff_end - notify via *Messages*, not the buffer
     ((and action (string= action "handoff_end"))
      (setq agent-shell-matrix-handoff--state nil)
      (message "✓ Session returned to Emacs"))
     
     ;; Regular message from user in Matrix - submit to agent via shell-maker
     (msg
      (when shell-buffer
        (let ((room-id (cdr (assoc "room_id" agent-shell-matrix-handoff--state))))
          (when room-id
            (agent-shell-matrix-handoff--set-typing room-id t)
            (setq agent-shell-matrix-handoff--typing t)))
        (with-current-buffer shell-buffer
          (shell-maker-submit :input msg)))))))

(defun agent-shell-matrix-handoff--relay-async (room-id session-id text)
  "Asynchronously relay TEXT to Matrix room via bot webhook.
Uses url-retrieve to avoid blocking the process filter."
  (let* ((url-request-method "POST")
         (url-request-extra-headers
          (list (cons "Authorization" (format "Bearer %s" agent-shell-matrix-webhook-secret))
                (cons "Content-Type" "application/json; charset=utf-8")))
         (url-request-data
          (encode-coding-string
           (json-encode (list (cons "room_id" room-id)
                              (cons "session_id" session-id)
                              (cons "response_text" text)))
           'utf-8)))
    (url-retrieve
     (concat agent-shell-matrix-bot-url "/webhook/message")
     (lambda (_status) (kill-buffer))
     nil t)))

(defun agent-shell-matrix-handoff--set-typing (room-id typing)
  "Asynchronously set typing indicator for ROOM-ID."
  (let* ((url-request-method "POST")
         (url-request-extra-headers
          (list (cons "Authorization" (format "Bearer %s" agent-shell-matrix-webhook-secret))
                (cons "Content-Type" "application/json; charset=utf-8")))
         (url-request-data
          (encode-coding-string
           (json-encode (list (cons "room_id" room-id)
                              (cons "typing" (if typing t :json-false))))
           'utf-8)))
    (url-retrieve
     (concat agent-shell-matrix-bot-url "/typing")
     (lambda (_status) (kill-buffer))
     nil t)))

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
  "Advice around agent-shell--on-notification to relay to Matrix during handoff."
  (apply orig-fun args)
  (when agent-shell-matrix-handoff--state
    (let* ((notification (plist-get args :notification))
           (update (map-elt (map-elt notification 'params) 'update))
           (session-update (and update (map-elt update 'sessionUpdate))))
      (cond
       ((equal session-update "agent_message_chunk")
        (let ((text (map-nested-elt update '(content text))))
          (when text
            (setq agent-shell-matrix-handoff--output-buffer
                  (concat agent-shell-matrix-handoff--output-buffer text))
            (when agent-shell-matrix-handoff--relay-timer
              (cancel-timer agent-shell-matrix-handoff--relay-timer))
            (setq agent-shell-matrix-handoff--relay-timer
                  (run-at-time 2.0 nil #'agent-shell-matrix-handoff--flush-output)))))
       ((equal session-update "tool_call")
        (let ((title (map-elt update 'title)))
          (when title
            (agent-shell-matrix-handoff--flush-output)
            (let ((room-id (cdr (assoc "room_id" agent-shell-matrix-handoff--state)))
                  (session-id (cdr (assoc "session_id" agent-shell-matrix-handoff--state))))
              (when room-id
                (agent-shell-matrix-handoff--relay-async
                 room-id session-id
                 (format "🔧 %s" title)))))))))))

(defun agent-shell-matrix-webhook--send-response (process status-code body)
  "Send HTTP response to PROCESS with STATUS-CODE and JSON BODY."
  (let ((response (format "HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                          status-code
                          (length body)
                          body)))
    (process-send-string process response)
    (delete-process process)))

(defun agent-shell-matrix-webhook--client-filter (process data)
  "Filter for webhook client connections."
  (let ((buffer (gethash process agent-shell-matrix-webhook-connections)))
    (unless buffer
      (setq buffer (generate-new-buffer " *webhook-client*"))
      (puthash process buffer agent-shell-matrix-webhook-connections))
    (with-current-buffer buffer
      (insert data)
      ;; Check if we have complete HTTP request (double CRLF)
      (when (string-match "\r\n\r\n" (buffer-string))
        (let ((payload (agent-shell-matrix-webhook--parse-http-body (buffer-string))))
          (if payload
              (progn
                (agent-shell-matrix-webhook--process-message payload)
                (agent-shell-matrix-webhook--send-response process 200 "{\"status\":\"ok\"}"))
            (agent-shell-matrix-webhook--send-response process 400 "{\"error\":\"Invalid JSON\"}")))
        (remhash process agent-shell-matrix-webhook-connections)
        (kill-buffer buffer)))))

(defun agent-shell-matrix-webhook--server-sentinel (server-process event)
  "Sentinel for accepting webhook connections.
When a client connects, server-process is the client connection."
  (let ((event-str (string-trim event)))
    (when (or (string= event-str "open") (string-match "^open" event-str))
      ;; A client has connected; server-process is actually the client connection
      (set-process-filter server-process 'agent-shell-matrix-webhook--client-filter))))

;;;###autoload
(defun agent-shell-matrix-webhook-start ()
  "Start the webhook server listening for matrix-proxy-bot calls."
  (interactive)
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
           :sentinel 'agent-shell-matrix-webhook--server-sentinel
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
                             (cons "webhook_secret" "test-secret")))
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
    
    (setq agent-shell-matrix-handoff--state nil)
    (message "✓ Session returned to Emacs")))

;; Install advice to relay agent output and tool calls to Matrix
(advice-add 'agent-shell--on-notification :around
            #'agent-shell-matrix-handoff--notification-advice)

(provide 'agent-shell-matrix-handoff)

;;; agent-shell-matrix-handoff.el ends here

