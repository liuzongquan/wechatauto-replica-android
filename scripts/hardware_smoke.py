"""显式授权的只读真机前置检查；缺少设备或条件时必须失败，不静默跳过。"""
from __future__ import annotations

import argparse
import json
import sys

from wechatauto_android.adapter import Uiautomator2Adapter
from wechatauto_android.controller import ACCOUNT_ROLES, WeChat
from wechatauto_android.errors import IdentityMismatch, PermissionDenied, WeChatError
from wechatauto_android.profile import Profile
from wechatauto_android.safety import SafetyPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Authorized device/account smoke only; navigates account pages, "
                     "never proves live business-feature parity")
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--confirm-authorized-device", action="store_true",
                        help="Confirm permission to read this device/account and navigate its UI")
    parser.add_argument("--allow-device-setup", action="store_true",
                        help="Permit uiautomator2 helper upload/start on this exact device")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.confirm_authorized_device or not args.allow_device_setup:
            raise PermissionDenied(
                "Both --confirm-authorized-device and --allow-device-setup are required"
            )
        profile = Profile.load(args.profile)
        if not profile.calibrated:
            raise PermissionDenied("Read-navigation smoke requires an operator-calibrated profile")
        profile.require(*ACCOUNT_ROLES)
        policy = SafetyPolicy(serial=args.serial, account_id=args.account, execute=False)
        adapter = Uiautomator2Adapter(args.serial, allow_device_setup=True)
        before = adapter.identity()
        policy.verify_device(before, profile.package, profile.version)
        if before.sdk not in profile.android_sdks:
            raise IdentityMismatch("Android SDK is not included in this exact profile")
        client = WeChat(adapter, profile, policy)
        proof = client.verify_account()
        after = adapter.identity()
        if before != after or proof.get("account_id") != args.account:
            raise IdentityMismatch("Identity changed or account evidence is unavailable")
        # 不输出序列号、账号、截图、消息或任何密钥；不上传证据。
        print(json.dumps({
            "schema_version": 1,
            "result": "passed_read_only_smoke",
            "wechat_version": after.version,
            "android_sdk": after.sdk,
            "account_verified": True,
            "ui_navigation_performed": True,
            "business_side_effects_requested": False,
            "live_business_tests_run": False,
            "root_snapshot_tested": False,
            "profile_calibration_is_operator_assertion": True,
        }, ensure_ascii=False))
        return 0
    except (WeChatError, OSError) as exc:
        print(json.dumps({"result": "failed_read_only_smoke", "error": type(exc).__name__,
                          "message": str(exc), "live_business_tests_run": False},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
