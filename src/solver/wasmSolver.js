import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let wasmModulePromise;

async function locateZ3Module() {
  if (process.env.SKIP_Z3 === '1') {
    return null;
  }

  if (wasmModulePromise !== undefined) {
    return wasmModulePromise;
  }

  const candidate = path.join(__dirname, 'vendor', 'z3.cjs');
  try {
    await fs.access(candidate);
    const require = createRequire(import.meta.url);
    const previousFetch = globalThis.fetch;
    const previousModule = globalThis.Module;
    globalThis.Module = {
      locateFile: (filename) => {
        if (filename === 'z3.worker.js') {
          return path.join(__dirname, 'vendor', 'z3.worker.cjs');
        }
        return path.join(__dirname, 'vendor', filename);
      }
    };
    if (typeof globalThis.fetch === 'function') {
      globalThis.fetch = undefined;
    }
    // Load as CommonJS to preserve __dirname/__filename inside the bundle.
    const loaded = require(candidate);
    wasmModulePromise = Promise.resolve(loaded).finally(() => {
      if (previousFetch !== undefined) {
        globalThis.fetch = previousFetch;
      } else {
        delete globalThis.fetch;
      }
      if (previousModule === undefined) {
        delete globalThis.Module;
      } else {
        globalThis.Module = previousModule;
      }
    });
  } catch (error) {
    console.warn(
      '[solver] Z3 wasm bundle not found at src/solver/vendor/z3.cjs. Set SKIP_Z3=1 to silence this message.'
    );
    wasmModulePromise = null;
  }

  return wasmModulePromise;
}

function runNativeZ3(code, options = {}) {
  const z3Path = options.z3Path || process.env.Z3_PATH || 'z3';
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    let spawnError = null;
    let resolved = false;
    const child = spawn(z3Path, ['-in'], {
      stdio: ['pipe', 'pipe', 'pipe']
    });

    child.stdin.on('error', (err) => {
      spawnError = err;
    });

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });

    child.on('error', (err) => {
      spawnError = err;
      if (resolved) {
        return;
      }
      resolved = true;
      resolve({
        status: 'error',
        runner: 'native',
        error: err.message,
        stdout: stdout.trim() || undefined,
        stderr: stderr.trim() || undefined
      });
    });

    child.on('close', (exitCode) => {
      if (spawnError) {
        if (resolved) return;
        resolved = true;
        resolve({
          status: 'error',
          runner: 'native',
          error: spawnError.message,
          stdout: stdout.trim() || undefined,
          stderr: stderr.trim() || undefined
        });
        return;
      }

      if (resolved) return;
      resolved = true;
      if (exitCode === 0) {
        resolve({
          status: 'ok',
          runner: 'native',
          result: stdout.trim(),
          stderr: stderr.trim() || undefined
        });
        return;
      }

      resolve({
        status: 'error',
        runner: 'native',
        error: `z3 exited with code ${exitCode}`,
        stdout: stdout.trim() || undefined,
        stderr: stderr.trim() || undefined
      });
    });

    child.stdin.end(code);
  });
}

export async function solveWithZ3(code, options = {}) {
  if (process.env.SKIP_Z3 === '1') {
    return { status: 'skipped', reason: 'SKIP_Z3 environment variable is set.' };
  }

  const useWasm = options.useWasm === true || process.env.Z3_USE_WASM === '1';

  if (useWasm) {
    const moduleLoader = await locateZ3Module();
    if (moduleLoader) {
      try {
        const moduleExports = await moduleLoader;
        if (typeof moduleExports.solve === 'function') {
          const result = moduleExports.solve(code, options);
          return { status: 'ok', runner: 'wasm', result };
        }
        if (typeof moduleExports.Module === 'function') {
          const instance = await moduleExports.Module({});
          if (typeof instance.solve === 'function') {
            const result = instance.solve(code, options);
            return { status: 'ok', runner: 'wasm', result };
          }
        }
        if (typeof moduleExports.default === 'function') {
          const instance = await moduleExports.default({});
          if (typeof instance.solve === 'function') {
            const result = instance.solve(code, options);
            return { status: 'ok', runner: 'wasm', result };
          }
        }
      } catch (error) {
        return { status: 'error', runner: 'wasm', error: error.message };
      }
    }
  }

  return runNativeZ3(code, options);
}
