/* Original WCDB2 observer, no copied community implementation.
 * API facts: Tencent/wcdb@17ebdcbb0a6d610432ea0106718a40676506b0a3
 * src/java/main/src/main/java/com/tencent/wcdb/core/Database.java
 * One/two-argument public setters delegate to this exact three-argument setter.
 * getPath() reads this database object's path; no constructor/global-first-key map.
 * setCipherKey configures a lazily opened DB: success is NOT decryption proof.
 * Host snapshot verification is mandatory. No live WeChat validation claimed.
 */
'use strict';
(function () {
    let sequence = 0;
    let identity = null;
    let stopped = false;
    const signature = ['[B', 'int', 'com.tencent.wcdb.core.Database$CipherVersion'];
    function failure() {
        if (stopped) return;
        stopped = true;
        try { send({protocol: 1, nonce: CAPTURE.nonce, kind: 'error', code: 'unsupported'}); }
        catch (_) { /* No application exception replacement or diagnostic logging. */ }
    }
    function emit(kind, extra, data) {
        send(Object.assign({protocol: 1, nonce: CAPTURE.nonce, kind: kind}, identity, extra), data);
    }
    if (typeof Java === 'undefined' || !Java.available || Frida.version !== CAPTURE.frida_version) {
        failure(); return;
    }
    Java.perform(function () {
        try {
            const activityThread = Java.use('android.app.ActivityThread');
            const app = activityThread.currentApplication();
            let context, packageName, processName;
            const uid = Java.use('android.os.Process').myUid();
            if (app !== null) {
                context = app.getApplicationContext();
                packageName = String(context.getPackageName());
                processName = String(Java.use('android.app.Application').getProcessName());
            } else {
                // Java.perform may run before Application creation during a spawn.
                // Read bound framework identity; never guess a package/loader or
                // bypass hidden-API restrictions when this exact API is absent.
                if (CAPTURE.mode !== 'spawn') { failure(); return; }
                packageName = String(activityThread.currentPackageName());
                processName = String(activityThread.currentProcessName());
                context = activityThread.currentActivityThread().getSystemContext();
            }
            const user = Math.floor(uid / 100000);
            const api = Java.use('android.os.Build$VERSION').SDK_INT.value;
            if (packageName !== 'com.tencent.mm' || processName !== 'com.tencent.mm') {
                failure(); return;
            }
            const info = context.getPackageManager().getPackageInfo('com.tencent.mm', 0);
            if (CAPTURE.mode === 'spawn' && info.applicationInfo.value.uid.value !== uid) {
                failure(); return;
            }
            const version = String(info.versionName.value);
            if (packageName !== 'com.tencent.mm' || processName !== 'com.tencent.mm' ||
                    version !== CAPTURE.version || user !== CAPTURE.android_user || api < 30) {
                failure(); return;
            }
            identity = {package: packageName, version: version, android_user: user,
                api: api, profile: CAPTURE.profile};
            const database = Java.use('com.tencent.wcdb.core.Database');
            const overload = database.setCipherKey.overload.apply(database.setCipherKey, signature);
            const getPath = database.getPath.overload();
            if (overload.returnType.className !== 'void' ||
                    getPath.returnType.className !== 'java.lang.String') {
                failure(); return;
            }
            overload.implementation = function (password, pageSize, cipherVersion) {
                let observed = null;
                let material = null;
                try {
                    if (!stopped && sequence < CAPTURE.max_candidates) {
                        const pathText = String(getPath.call(this));
                        if (CAPTURE.paths.indexOf(pathText) !== -1) {
                            if (password === null || cipherVersion === null || password.length < 1 ||
                                    password.length > CAPTURE.max_key_bytes) {
                                failure();
                            } else {
                                const versionName = String(cipherVersion.name());
                                const versions = {version1: 1, version2: 2, version3: 3, version4: 4};
                                let compatibility = null;
                                if (versionName !== 'defaultVersion') {
                                    if (!Object.prototype.hasOwnProperty.call(versions, versionName)) {
                                        throw new Error('unsupported');
                                    }
                                    compatibility = versions[versionName];
                                }
                                // Defaults/global custom config are not guessed here. The host
                                // must supply every cipher setting and validate actual DB bytes.
                                observed = {path: pathText, cipher: {page_size: pageSize,
                                    compatibility: compatibility}};
                                material = new Uint8Array(password.length);
                                for (let i = 0; i < password.length; i++) material[i] = password[i] & 255;
                            }
                        }
                    }
                } catch (_) { failure(); }
                let result;
                try { result = overload.call(this, password, pageSize, cipherVersion); }
                catch (originalError) {
                    if (material !== null) material.fill(0);
                    throw originalError;
                }
                try {
                    if (!stopped && observed !== null && material !== null) {
                        observed.seq = ++sequence;
                        emit('candidate', observed, material.buffer);
                    }
                } catch (_) { failure(); }
                finally { if (material !== null) material.fill(0); }
                return result;
            };
            emit('ready', {signature: signature}, null);
        } catch (_) { failure(); }
    });
})();
