# Tasks revision WebSocket

The optional socket sends only the same revision value as the existing revision endpoint. The browser fetches authorized data through the current HTTP routes. A command committed to the selected ledger reaches the socket on its next one-second check. Reconnecting receives the current revision. The browser retains the selected fragment and uses its five-second HTTP check whenever a socket connection has yet to open or has closed.

Install the optional server on a disposable machine with `uv sync --frozen --extra websocket`. Use the existing portal-reader mode and add `--websocket-revisions` to enable the socket server. The default Waitress path still serves existing installs. In the optional mode, Gunicorn uses one worker for the current in-memory sessions and 16 threads for connections and HTTP requests. The listener is bound before opening the selected ledger.

## Portal preflight

Before enabling the flag on a deployment, check each proxy hop. Forward the full `/tasks/api/v1/revisions` WebSocket upgrade for a reader who is already admitted. Use the same trusted app header used for Tasks HTTP requests, and preserve the browser Origin. The app checks both the portal admission and exact configured origin before upgrading. Connections are refreshed periodically to recheck admission. Portal connections receive a revision value. Personal feed, decision and task APIs retain their own existing rules.

Verify the upgrade and any content policy from the actual canonical HTTPS board with an approved synthetic browser run. Keep a task selected in an open tab, commit a fixture change, and check that it appears without navigation. Interrupt the socket, verify the HTTP refresh, and restore the socket. Check the ordinary portal reader and verified sign-in flows over the same mount. A failed upgrade leaves five-second polling available. The release stays in polling mode until the exact app and portal review passes.

Roll back by disabling the server flag against the same selected ledger. Preserve every committed task, decision and attention record.
