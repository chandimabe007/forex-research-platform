"""Web UI: localhost dashboard and configuration editor (stdlib servers).

Read-only status dashboard binding 127.0.0.1:8787 and a form-based
configuration editor binding 127.0.0.1:8788. Neither asks for or stores
account credentials; the editor writes only the two fixed config files and
validates every save through the platform's own loaders.
"""
