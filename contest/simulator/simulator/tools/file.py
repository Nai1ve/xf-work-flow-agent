"""
Simulated file system tool for listing files in directories.
"""


class FileTool:
    def __init__(self, state: dict):
        self._state = state

    def list(self, args: dict) -> dict:
        """List files in a directory."""
        directory = args.get("directory", "")
        if not directory:
            return {"error": "directory parameter is required"}

        # Get files from simulated filesystem
        filesystem = self._state.get("filesystem", {})

        # Support both direct directory key and nested structure
        if directory in filesystem:
            files = filesystem[directory]
        else:
            # Try to find in nested structure
            files = None
            for key, value in filesystem.items():
                if key == directory or key.endswith(f"/{directory}"):
                    files = value
                    break

            if files is None:
                return {"error": f"Directory not found: {directory}"}

        return {
            "directory": directory,
            "files": files if isinstance(files, list) else []
        }
