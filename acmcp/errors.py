class AcmcpError(Exception):
    """Any user-facing failure: config, GitHub API, download, or mcp.json conflict."""
