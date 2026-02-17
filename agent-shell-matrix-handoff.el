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

(defvar agent-shell-matrix-handoff-context-lines 0
  "Number of lines to capture from agent-shell buffer for Matrix context.
Set to 0 to disable context replay.")

(defvar agent-shell-matrix-webhook-secret "REDACTED-SET-VIA-CUSTOMIZE"
  "Bearer token for authenticating with matrix-proxy-bot.")

(defvar agent-shell-matrix-bot-url "http://127.0.0.1:8765"
  "URL of the matrix-proxy-bot server.")

(defvar agent-shell-matrix-webhook-port 9999
  "Port for the webhook server.")

(defvar agent-shell-matrix-webhook-server-process nil
  "Server process listening for webhook calls.")

(defvar agent-shell-matrix-webhook-connections nil
  "Hash table of active connections: process -> buffer.")

(defun agent-shell-matrix-handoff--capture-context (buffer)
  "Capture recent history from BUFFER for Matrix context.
Returns the last N lines as a string, or empty if disabled."
  (if (<= agent-shell-matrix-handoff-context-lines 0)
      ""
    (with-current-buffer buffer
      (let ((end (point-max))
            (start (max 1 (- (point-max) 
                            (* agent-shell-matrix-handoff-context-lines 80)))))
        (string-trim (buffer-substring-no-properties start end))))))

(defun agent-shell-matrix-handoff--call-bot (endpoint method data)
  "Call matrix-proxy-bot ENDPOINT with METHOD and DATA.
Returns parsed JSON response."
  (let ((url-request-method method)
        (url-request-extra-headers
         (list (cons "Authorization" (format "Bearer %s" agent-shell-matrix-webhook-secret))
               (cons "Content-Type" "application/json")))
        (url-request-data (and data (json-encode data))))
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
        (response-text (alist-get 'response_text payload))
        (shell-buffer (and agent-shell-matrix-handoff--state
                          (cdr (assoc "shell_buffer" agent-shell-matrix-handoff--state)))))
    
    (cond
     ;; Command: handoff_end - notify via *Messages*, not the buffer
     ((and action (string= action "handoff_end"))
      (setq agent-shell-matrix-handoff--state nil)
      (message "✓ Session returned to Emacs"))
     
     ;; Regular message from user in Matrix - insert and simulate RET keystroke
     (msg
      (when shell-buffer
        (with-current-buffer shell-buffer
          (goto-char (point-max))
          (insert msg)
          ;; Simulate RET keystroke to trigger agent
          (execute-kbd-macro (kbd "RET")))))
     
     ;; Response from agent relay
     (response-text
      (when shell-buffer
        (with-current-buffer shell-buffer
          (insert (format "[Agent] %s\n" response-text))))))))

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
           :host "127.0.0.1"
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
                             (cons "webhook_url" "http://127.0.0.1:9999/webhook")
                             (cons "webhook_secret" "test-secret")))
         (handoff-data (if (and context (not (string-empty-p context)))
                          (append handoff-data (list (cons "message" (format "Context (replay):\n```\n%s\n```" context))))
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
  
  (let ((room-id (alist-get "room_id" agent-shell-matrix-handoff--state))
        (session-id (alist-get "session_id" agent-shell-matrix-handoff--state)))
    
    (agent-shell-matrix-handoff--call-bot
     "/webhook/message"
     "POST"
     (list (cons "room_id" room-id)
           (cons "session_id" session-id)
           (cons "action" "handoff_end")))
    
    (setq agent-shell-matrix-handoff--state nil)
    (message "✓ Session returned to Emacs")))

(provide 'agent-shell-matrix-handoff)

;;; agent-shell-matrix-handoff.el ends here

