"""默认无副作用的 CLI；真实执行必须逐目标显式授权。"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import __version__
from .controller import WeChat, capabilities
from .errors import ValidationError, WeChatError
from .profile import Profile, Target
from .safety import SafetyPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechatauto-android", description="Android WeChat: unverified UI profiles fail closed")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command", required=True)
    caps = subs.add_parser("capabilities", help="Report code state, not a live-test claim")
    caps.add_argument("--profile")
    check = subs.add_parser("profile-check", help="Validate selector profile without a device")
    check.add_argument("profile")
    doctor = subs.add_parser("doctor", help="Read device metadata; no login/unlock or sending")
    doctor.add_argument("--serial", required=True)
    doctor.add_argument("--profile", required=True)
    doctor.add_argument("--allow-device-setup", action="store_true")
    snap = subs.add_parser("snapshot", help="Capture private UI XML; may contain personal data")
    snap.add_argument("--serial", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--allow-device-setup", action="store_true")
    send = subs.add_parser("send", help="Plan text sending; --execute requires explicit allowlist")
    for name in ("serial", "profile", "account", "chat-id", "title", "text", "journal"):
        send.add_argument("--" + name, required=True)
    send.add_argument("--allow-device-setup", action="store_true")
    send.add_argument("--assume-exclusive-device", action="store_true", help="Explicitly accept prior account proof only; requires exclusive device and no account switching")
    send.add_argument("--execute", action="store_true")
    send.add_argument("--allow-target", action="append", default=[])
    send.add_argument("--operation-id", default="dry-run-only")
    archive = subs.add_parser("archive", help="Read a user-supplied normalized export, NOT an Android DB")
    archive.add_argument("--file", required=True)
    archive.add_argument("--account", required=True)
    modes = archive.add_subparsers(dest="archive_command", required=True)
    msgs = modes.add_parser("messages")
    msgs.add_argument("chat")
    msgs.add_argument("--limit", type=int, default=20)
    sessions = modes.add_parser("sessions")
    sessions.add_argument("--limit", type=int, default=100)
    return parser


def _dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=lambda obj: obj.to_dict()))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capabilities":
            _dump(capabilities(Profile.load(args.profile) if args.profile else None))
        elif args.command == "profile-check":
            profile = Profile.load(args.profile)
            _dump({"valid": True, "name": profile.name, "version": profile.version,
                   "android_sdks": profile.android_sdks, "calibrated": profile.calibrated,
                   "live_tested_by_this_project": False})
        elif args.command == "archive":
            from .archive import ExportArchive
            archive = ExportArchive.open(args.file, account_id=args.account)
            if args.archive_command == "messages":
                _dump(archive.get_messages(args.chat, limit=args.limit))
            else:
                _dump(archive.get_sessions(limit=args.limit))
        elif args.command == "doctor":
            from .adapter import Uiautomator2Adapter
            profile = Profile.load(args.profile)
            adapter = Uiautomator2Adapter(args.serial, allow_device_setup=args.allow_device_setup)
            identity = adapter.identity()
            if (identity.version != profile.version or identity.package != profile.package
                    or (profile.calibrated and identity.sdk not in profile.android_sdks)):
                raise ValidationError("Exact profile version/package does not match device")
            _dump({"device": asdict(identity), "profile": profile.name,
                   "profile_calibrated": profile.calibrated,
                   "account_verified": False, "live_feature_tests_run": False})
        elif args.command == "snapshot":
            from .adapter import Uiautomator2Adapter
            adapter = Uiautomator2Adapter(args.serial, allow_device_setup=args.allow_device_setup)
            # XML isn't logged. Use an exclusive new private output, never overwrite an existing file.
            adapter.save_hierarchy(Path(args.out))
            _dump({"saved": args.out, "contains_private_data": True})
        elif args.command == "send":
            profile = Profile.load(args.profile)
            policy = SafetyPolicy(args.serial, args.account,
                                  allowed_targets=frozenset(args.allow_target),
                                  allowed_actions=frozenset({"send_text"}) if args.execute else frozenset(),
                                  execute=args.execute, assume_exclusive_device=args.assume_exclusive_device)
            if args.execute and args.operation_id == "dry-run-only":
                raise ValidationError("--execute needs a unique --operation-id")
            target = Target(args.chat_id, args.title)
            plan = WeChat(None, profile, replace(policy, execute=False), args.journal).send_text(
                target, args.text, operation_id=args.operation_id)
            adapter = None
            # Run local config/permission checks before even initializing the device SDK.
            if args.execute:
                policy.authorize("send_text", args.chat_id)
                if not profile.calibrated:
                    raise ValidationError("Cannot execute an uncalibrated profile")
                if "commit.account_identity" not in profile.roles and not args.assume_exclusive_device:
                    raise ValidationError("Strict mode needs final account proof; --assume-exclusive-device explicitly accepts weaker prior proof")
                from .adapter import Uiautomator2Adapter
                adapter = Uiautomator2Adapter(args.serial, allow_device_setup=args.allow_device_setup)
            if not args.execute:
                _dump(plan)
                return 0
            client = WeChat(adapter, profile, policy, args.journal)
            _dump(client.send_text(target, args.text,
                                   operation_id=args.operation_id))
        return 0
    except (WeChatError, OSError) as exc:
        # 不输出原始 SDK 内容、密钥、完整消息或堆栈。
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
