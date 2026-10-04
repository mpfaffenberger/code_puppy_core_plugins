"""User-only consent-file permissions on POSIX and Windows."""

from __future__ import annotations

import os
from pathlib import Path

from .backend_types import ComputerUseError


def secure_policy_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        acl = win32security.ACL()
        for sid in (user, system):
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION, 0, ntsecuritycon.FILE_ALL_ACCESS, sid
            )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    except Exception as exc:
        raise ComputerUseError(
            "Could not restrict Windows consent-file permissions; install the computer-use extra "
            f"and check filesystem ACL support: {exc}"
        ) from exc
