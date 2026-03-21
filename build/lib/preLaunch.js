"use strict";
/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
// @ts-check
const path_1 = __importDefault(require("path"));
const child_process_1 = require("child_process");
const fs_1 = require("fs");
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const rootDir = path_1.default.resolve(__dirname, '..', '..');
const gulpScript = path_1.default.join(rootDir, 'node_modules', 'gulp', 'bin', 'gulp.js');
function runProcess(command, args = []) {
    return new Promise((resolve, reject) => {
        const child = (0, child_process_1.spawn)(command, args, { cwd: rootDir, stdio: 'inherit', env: process.env, shell: process.platform === 'win32' });
        child.on('exit', err => !err ? resolve() : process.exit(err ?? 1));
        child.on('error', reject);
    });
}
async function exists(subdir) {
    try {
        await fs_1.promises.stat(path_1.default.join(rootDir, subdir));
        return true;
    }
    catch {
        return false;
    }
}
async function ensureNodeModules() {
    if (!(await exists('node_modules'))) {
        await runProcess(npm, ['ci']);
    }
}
async function getElectron() {
    await runProcess(npm, ['run', 'electron']);
}
async function ensureCompiled() {
    const requiredOutputs = [
        'out/vs/workbench/workbench.desktop.main.js',
        'out/vs/workbench/contrib/proton9/electron-sandbox/proton9.contribution.js',
    ];
    const missingOutput = !(await Promise.all(requiredOutputs.map(exists))).every(Boolean);
    if (missingOutput) {
        await runProcess(npm, ['run', 'compile']);
    }
}
async function ensureExtensionsCompiled() {
    const requiredOutputs = [
        'extensions/git-base/out/extension.js',
        'extensions/git-base/dist/browser/extension.js',
        'extensions/emmet/out/node/emmetNodeMain.js',
        'extensions/emmet/dist/browser/emmetBrowserMain.js',
    ];
    const missingOutput = !(await Promise.all(requiredOutputs.map(exists))).every(Boolean);
    if (missingOutput) {
        await runProcess(process.execPath, [gulpScript, 'compile-extensions']);
    }
}
async function main() {
    await ensureNodeModules();
    await getElectron();
    await ensureCompiled();
    await ensureExtensionsCompiled();
    // Can't require this until after dependencies are installed
    const { getBuiltInExtensions } = require('./builtInExtensions');
    await getBuiltInExtensions();
}
if (require.main === module) {
    main().catch(err => {
        console.error(err);
        process.exit(1);
    });
}
//# sourceMappingURL=preLaunch.js.map