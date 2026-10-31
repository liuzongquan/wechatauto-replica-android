# Independent QA review

## Evidence boundaries

- Reference: `fanyuantaier/wechatauto-replica`, commit `bc2e6f2461330eb1a53996d2f2afc18416376b52`.
- A synthetic XML hierarchy, fake SDK, user-authored archive, or stub transport test is **offline unit/contract evidence only**, not an Android or WeChat end-to-end test.
- No Android device, emulator, ADB connection, real WeChat account, real contacts, live messages, or live media have been used in this review.
- The current user-approved phase is cloud development, offline tests/CI and GitHub publication. Local-computer setup has been deferred; complete functional parity and live acceptance remain deferred, not passed.
- User-supplied archive queries and copying already-exported media are not equivalent to extracting WeChat Android's private database/media.
- A display title (even a unique exact title) is not a stable contact ID. Account and target ID proof must be separately established; unavailable IDs are a blocker, not permission to guess.
- A successful UI click or text entry is not remote delivery. `verified` must mean the documented observable postcondition, not recipient delivery/read receipt.

## Mandatory adversarial coverage

1. Exact device serial + package + installed version; device disconnect/account switch/app switch invalidates stale authorization.
2. Account ID and target ID proof, duplicate selectors, foreign package overlay, literal Unicode identity (no trim/casefold/NFKC fuzzy target acceptance).
3. Default dry-run and deny-by-default policy; action and exact target scope; preview cannot type, click, push or send.
4. Payload/profile/identity freshness between preview and execution; reject changed scope rather than silently broadening approval.
5. Durable operation ID before a commit; exception/timeout after commit cannot automatically retry; recovery must report outcome unknown.
6. Input readback, failed SDK return, selected recipient verification, and no false `verified` based on unchanged pre-existing UI.
7. Histories distinguish account+chat+message ID; no fabricated sender identity/sequence/timestamp; source provenance remains explicit.
8. Listener ties, callback failure, persistence/restart, account mismatch, duplicate registration, disconnect and bounded shutdown.
9. Media traversal, absolute path, symlink parent/leaf escapes, same-file overwrite, checksum failure and no app-private extraction.
10. Real-device acceptance remains blocked until a calibrated version-specific profile and authorized test device/account/recipients exist.

## Root and compatibility scope

The user has allowed root and targets WeChat app 8.0+ on Android 11/API 30+ (interpreted from Android systems after 2020). This permits implementing explicit rooted snapshot/import backends; it does not supply a test device, credentials, account/recipient authorization, or proof that every 8.x version is compatible. DB/WAL consistency, exact device/account binding, known schema/cipher parameters, safe paths, and key redaction require separate tests. Unknown versions/schemas must fail closed.

## SDK caveats independently checked

The upstream `uiautomator2` implementation retries JSON-RPC calls after certain transport/server failures, including after restarting its server. Therefore an application-level journal alone does not establish a strict end-to-end at-most-once guarantee. `UiObject.click()` also does not propagate the low-level click return value, while `set_text()` returns its RPC result. The adapter pins SDK 3.7.1 and overrides the retrying entrypoint with a single lower-level RPC call; installed-SDK contract tests cover this mitigation without connecting a device. External touches, overlays, or account changes still create a non-atomic observation/click race.

Sources inspected: [uiautomator2 core.py](https://github.com/openatx/uiautomator2/blob/master/uiautomator2/core.py), [selector implementation](https://github.com/openatx/uiautomator2/blob/master/uiautomator2/_selector.py).

## Confirmed fixes and remaining limits

Independent regression tests reproduced and then verified fixes for:

- forged device provenance in imported contact/group/label/Moments records;
- ignored explicit false navigation/back acknowledgments;
- listener callbacks continuing after stop/removal;
- a dangling journal symlink creating a file outside the intended journal path;
- post-open database changes being emitted under an old snapshot fingerprint;
- duplicate native message IDs invalidating lossless cursor assumptions;
- Android SDK metadata drifting during a root snapshot;
- interrupted Frida script unloading skipping the session-detach attempt;
- a double-tap issuing its second RPC after detectable hierarchy/account drift;
- reply/recall proceeding after an explicit false long-press acknowledgment;
- uncalibrated Moments observation pressing Back before its calibration gate;
- same-text forward/reply/recall selection after visible source-chat title drift.

Attachment content is now staged from the originally hashed bytes, preventing a source-path replacement during navigation from silently changing the upload. SDK initialization requires explicit device-setup consent because construction can install/start the helper.

**Account drift was reproduced and strict writes now fail closed:** device/package/version checks alone do not reveal a later account switch. Default execution now requires a fresh account marker in the commit snapshot or an independent account verifier. A caller can explicitly acknowledge the weaker exclusive-device assumption; its result labels that weaker assurance. Neither path makes observation/click atomic, and no real WeChat test has established a supported session signal/profile. Visible reads now require the same proof and retain the assurance label. Profiles/callbacks are trusted operator configuration, not remote account attestation.

## Results at the independent review point

2026-10-31 18:51 UTC, Python 3.12, with the pinned optional dependencies installed:

- **783 tests passed, zero skipped** in the complete offline suite.
- **103 independent adversarial tests** in `tests/test_adversarial_safety.py`.
- Ruff checks passed across the repository.
- SDK pins inspected: uiautomator2 3.7.1, Frida 16.7.19, sqlcipher3-binary 0.5.4.
- Genuine SQLCipher *synthetic* fixtures cover successful decryption, wrong key/cipher, integrity rejection, bound-key reuse, an uncheckpointed encrypted WAL row, and exact binary passwords including NUL/non-UTF-8 at the supported 1–40-byte boundary. Candidate verification uses a private encrypted-bundle copy, preserving original snapshot bytes.
- Key-capture tests use fake Frida lifecycle/events, a simulated Java bridge executed with Node, and candidate validation against a synthetic encrypted database. They do not capture a real WeChat key.
- Independent UI regressions cover rejection of unbound generic title selectors, forged/stale hierarchy-node references, scoped Moments observations, duplicate controls, exact forward source/destination and mention-member permissions, dry-run callback isolation, and avoiding editor rewrites after a synthetic native mention selection. The mock proves control flow, not the existence/preservation of a real WeChat native @ span.
- Optional Frida spawn is code-only/offline tested: separate attach, experimental-profile, package stop/relaunch and system-instrumentation consents; exact user-0 package metadata; framework UID/version gates; load/resume ordering; interruption cleanup; no key returned after uncertain cleanup. Its idle-app check cannot make Frida’s subsequent force-stop atomic; detach does not attest restoration of Frida-owned system helpers.
- Hardware-smoke guard tests check refusal/argument handling only; no live smoke was run.

Re-run the complete checks after any subsequent change. A passing offline review is **not a release certificate for complete parity**. No Android device/emulator/ADB/root/Frida endpoint was connected, and no account, recipient, UI profile, database key or WeChat version was live-tested. Full functionality/compatibility acceptance remains blocked by the missing authorized device/version/profile evidence and the explicit unavailable rows in [FEATURE_MATRIX.md](FEATURE_MATRIX.md).

Publication, CI on the published commit, package installation and container deployment are separate checks owned by the task integrator; this report does not assert they have succeeded.
