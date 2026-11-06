"""ACP extension methods that expose the approved-folder policy to clients.

The SDK strips the leading ``_`` from extension names and routes them to
``CodePuppyAgent.ext_method``. Clients read and edit the user's folder list
through these methods; they never write ``puppy.cfg`` themselves.

Called by: agent.py :: CodePuppyAgent.ext_method()
Depends on: file_permission_handler/folder_policy.py
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from acp import RequestError

from code_puppy_core_plugins.file_permission_handler import folder_policy

GET_METHOD = "code_puppy/approved_folders/get"
ADD_METHOD = "code_puppy/approved_folders/add"
REMOVE_METHOD = "code_puppy/approved_folders/remove"
METHOD_NAMES = (GET_METHOD, ADD_METHOD, REMOVE_METHOD)


def handle(
    method: str, params: Mapping[str, Any], sessions: Mapping[str, Any]
) -> dict[str, Any]:
    """Dispatch one extension call. ``sessions`` maps session ids to objects with ``.cwd``."""
    if method not in METHOD_NAMES:
        raise RequestError.method_not_found(f"_{method}")
    cwd = _session_cwd(params, sessions)
    try:
        if method == ADD_METHOD:
            folder_policy.add_user_folder(_path(params))
        elif method == REMOVE_METHOD:
            folder_policy.remove_user_folder(_path(params))
    except folder_policy.PolicyError as error:
        raise _invalid_params(str(error)) from error
    return _snapshot(cwd)


def _snapshot(cwd: Optional[str]) -> dict[str, Any]:
    always = folder_policy.always_approved_folders(cwd)
    return {
        "projectFolder": always["project"],
        "tempFolders": list(always["temp"]),
        "userFolders": folder_policy.get_user_folders(),
        "protectedPaths": folder_policy.protected_paths(),
    }


def _session_cwd(
    params: Mapping[str, Any], sessions: Mapping[str, Any]
) -> Optional[str]:
    """Return the cwd of ``params["sessionId"]``, or None to use the process cwd."""
    session_id = params.get("sessionId")
    if session_id is None:
        return None
    if not isinstance(session_id, str):
        raise _invalid_params("sessionId must be a string")
    session = sessions.get(session_id)
    if session is None:
        raise _invalid_params(f"unknown session: {session_id}")
    return session.cwd


def _path(params: Mapping[str, Any]) -> str:
    path = params.get("path")
    if not isinstance(path, str):
        raise _invalid_params("path must be a string")
    return path


def _invalid_params(message: str) -> RequestError:
    # The SDK's invalid_params() fixes the top-level message, so the reason is
    # carried in data where clients can read it.
    return RequestError(-32602, message, {"message": message, "reason": message})
