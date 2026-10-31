/* Original, minimal observer. CAPTURE is supplied by the host with no key material.
 * No RPC/export, file access, logging, memory scanning, native interception, launch,
 * key modification or database-opening call. Unknown overloads are not guessed.
 * This asset has only simulated contract coverage, not live WeChat validation.
 */
'use strict';
(function () {
    let sequence = 0;
    let identity = null;
    let stopped = false;
    const signature = [
        'java.lang.String', '[B', 'com.tencent.wcdb.database.SQLiteCipherSpec',
        'com.tencent.wcdb.database.SQLiteDatabase$CursorFactory', 'int',
        'com.tencent.wcdb.DatabaseErrorHandler', 'int'
    ];
    function failure() {
        if (stopped) return;
        stopped = true;
        try { send({protocol: 1, nonce: CAPTURE.nonce, kind: 'error', code: 'unsupported'}); }
        catch (_) { /* Never replace an application exception or print diagnostics. */ }
    }
    function emit(kind, extra, data) {
        const payload = Object.assign({protocol: 1, nonce: CAPTURE.nonce, kind: kind},
            identity, extra);
        send(payload, data);
    }
    function field(object, name) {
        const value = object[name];
        return value === undefined ? null : value.value;
    }
    function algorithm(spec, klass, name, prefix) {
        const value = field(spec, name);
        if (value === null) return null; // Old public WCDB API has no such field.
        for (const suffix of ['SHA1', 'SHA256', 'SHA512']) {
            const constant = field(klass, 'HMAC_' + suffix);
            if (constant !== null && value === constant) return prefix + suffix;
        }
        throw new Error('unsupported');
    }
    if (typeof Java === 'undefined' || !Java.available || Frida.version !== CAPTURE.frida_version) {
        failure();
        return;
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
            const database = Java.use('com.tencent.wcdb.database.SQLiteDatabase');
            const cipherClass = Java.use('com.tencent.wcdb.database.SQLiteCipherSpec');
            const overload = database.openDatabase.overload.apply(database.openDatabase, signature);
            if (overload.returnType.className !== 'com.tencent.wcdb.database.SQLiteDatabase') {
                failure(); return;
            }
            overload.implementation = function (path, password, spec, factory, flags, handler, pool) {
                let observed = null;
                let material = null;
                // Copy only this exact authorized DB's input, before the original can mutate it.
                // Observation failures NEVER affect the application's original invocation.
                try {
                    const pathText = path === null ? '' : String(path);
                    if (!stopped && sequence < CAPTURE.max_candidates &&
                            CAPTURE.paths.indexOf(pathText) !== -1) {
                        if (password === null || spec === null || password.length < 1 ||
                                password.length > CAPTURE.max_key_bytes) {
                            failure();
                        } else {
                            const cipher = field(spec, 'cipher');
                            observed = {path: pathText, cipher: {
                                cipher: cipher === null ? null : String(cipher),
                                page_size: field(spec, 'pageSize'),
                                kdf_iterations: field(spec, 'kdfIteration'),
                                use_hmac: field(spec, 'hmacEnabled'),
                                kdf_algorithm: algorithm(spec, cipherClass, 'kdfAlgorithm', 'PBKDF2_HMAC_'),
                                hmac_algorithm: algorithm(spec, cipherClass, 'hmacAlgorithm', 'HMAC_'),
                                compatibility: field(spec, 'cipherVersion')
                            }};
                            material = new Uint8Array(password.length);
                            for (let i = 0; i < password.length; i++) material[i] = password[i] & 255;
                        }
                    }
                } catch (_) { failure(); }
                let result;
                try {
                    // No catch/rewrite of the application's exception; exact original arguments.
                    result = overload.call(this, path, password, spec, factory, flags, handler, pool);
                } catch (originalError) {
                    if (material !== null) material.fill(0);
                    throw originalError;
                }
                try {
                    if (!stopped && observed !== null && material !== null) {
                        sequence += 1;
                        observed.seq = sequence;
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
