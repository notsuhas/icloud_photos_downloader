# Web UI

```{versionadded} 1.21.0
```

`icloudpd` can start an internal web server on port 8080 and accept input (password and MFA code) from there instead of the console. The web server is started only if `webui` is selected for [MFA provider and/or Password Provider](authentication).

Use `--defer-auth --mfa-provider webui` to pause before password login when the saved session expires or no session exists. Configure SMTP or `--notification-script` on the downloader itself to receive a notification. Open the Web UI and click **Start login** when you are ready; only then does the downloader sign in and request a code from Apple.

If the code expires, click **Request a new code** to start a fresh login and request another code without restarting the container. After a valid code, downloads resume. The page updates automatically while requesting or checking a code. Notifications are sent once per pending login; failed delivery is retried every minute. Restarting the process starts a new notification cycle.
