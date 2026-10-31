# Architecture and provenance boundaries

This is a Python **host controller**, not an Android APK or a Windows client port. The compatibility target is Android 11/API 30 and newer, running WeChat 8.0 and newer. An exact app-version/SDK profile is required for live UI work. No real device/version pair has been certified in this repository.

## Independent routes

| Route | Modules | What the evidence means |
|---|---|---|
| Visible Android UI | `ui`, `adapter`, `profile`, `controller` | Exact current-screen observations and bounded UI transitions; not complete history or database IDs |
| Rooted snapshots | `rooted` | User-authorized, already-rooted, quiescent snapshot; validated known EnMicroMsg schema and explicit SQLCipher configuration |
| Experimental key observation | `keycapture` | Separate process-attachment consent; a candidate is usable only after actual database verification and binding |
| User-provided exports | `archive`, `listener`, `media` | Explicit normalized/local data only; not evidence of Android extraction |

These origins are not interchangeable. Snapshot metadata and a schema's own-account field do not authenticate an arbitrary supplied database as a particular connected account. Export cursors do not establish real-time Android listening.

## UI state machine and safety

The controller starts from a known WeChat home screen, reads the own-account profile, searches an exact target, rejects multiple matches, and verifies the target's stable ID in its information page. A chat title alone is never treated as a `wxid` or group `@chatroom` ID. Missing identity evidence fails closed.

Text is entered through the accessibility RPC and read back byte-for-byte as Unicode. Immediately before text submission the target information is checked again and the complete draft is reread. Reply, recall, calls, double-tap poke, native member mentions, single-recipient forwarding, remarks and attachments have separate bounded transitions. Moments use explicitly authorized snapshot-scoped row references, not inferred native SNS IDs or author wxids. The irreversible recall menu/call option/send button is the final commit, not a preparatory click. There is no generic arbitrary action-script runner.

**Strict mode additionally requires fresh own-account proof at commit**: a `commit.account_identity` selector containing `{account_id}`, or a separately supplied `account_verifier` callback. Ordinary chat UI may not expose this proof. `SafetyPolicy.assume_exclusive_device=True` / `--assume-exclusive-device` explicitly allows the weaker prior-account-proof route and assumes no account switching, human interaction, or other automation. Every result reports its `assurance`. This assumption is not continuous identity validation.

Per-device in-process and same-OS-user process locks serialize operations. They cannot lock a human's finger or another host. A snapshot fingerprint detects observed UI changes before dispatch, but Android UI observation plus tapping is **not atomic**.

The SQLite journal reserves every operation ID, then durably marks `committing` before the one bounded final action. A poke deliberately uses two consecutive tap RPCs; this is the gesture, not a retry. Unknown outcomes are never automatically retried. The pinned uiautomator2 adapter disables the SDK's automatic JSON-RPC recovery/retry. A successfully acknowledged RPC reports `submitted`, **not delivered**. Visible postconditions, if used, are not server receipts. Operation IDs and their journal must be retained and reused across recovery decisions; deleting a journal removes that protection.

## Profiles

`Profile` contains exact WeChat `version`, exact `android_sdks`, named exact selectors, collections, operator calibration evidence, and group-identity support. `ProfileRegistry` never falls back to another release or guesses that the latest 8.x profile works. A calibration flag is an operator assertion, not this project's live-test certification.

The bundled Chinese semantic draft deliberately omits unobserved resource IDs and identity/navigation selectors. It cannot send. Synthetic test profiles use explicit `synthetic_` resources and must never be described as device captures.

## Files and secrets

Authorized attachment bytes are snapshotted before navigation, hashed, staged privately, then verified again at adapter upload. ADB transfer is limited to the dedicated shared-storage directory; system document-provider UI is not silently allowed. Snapshot/export/media paths have independent traversal, symlink, size and overwrite controls.

Database keys are not authentication tokens. Key material is supplied/observed in memory, tested by actual decryption/readability, SQLite integrity and schema checks against the selected database/configuration, and bound to account, database bundle fingerprint, version and cipher profile. For HMAC-off historical profiles these checks are not cryptographic authentication or provenance proof. Unknown encryption or schema is an explicit error. No automatic rooting, device unlock, DLL patching, or account login is implemented. Key observation defaults to attach-only. Optional experimental spawn capture separately requires explicit package-stop/relaunch and system-server/zygote instrumentation consent, an already-stopped app, and narrow user-0 scope; it is not a read-only root operation. No such device operation was executed during development.

## Testing boundaries

Deterministic tests cover synthetic hierarchy transitions, policy failures, Unicode, ambiguous selectors, uncertain sends, cross-account isolation, state changes, path attacks, source-scoped cursors, real SDK interfaces, and real encrypted SQLCipher fixture databases. They do not establish WeChat live-feature correctness. `scripts/hardware_smoke.py` is opt-in, fails missing prerequisites, and performs only device/account read-navigation smoke; it is not full business-function acceptance.
