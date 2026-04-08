#!/usr/bin/env node
/**
 * clawhub — OpenClaw skill manager
 *
 * Usage:
 *   clawhub search [query]
 *   clawhub install <slug>[@version]
 *   clawhub list
 *   clawhub rollback <slug> [@version]
 *   clawhub versions <slug>
 *
 * Options:
 *   --skills-dir <path>   Skills directory (default: ./skills)
 *   --registry <url>      Registry URL (default: https://clawhub.ai)
 *   --dry-run             Preview without writing files
 *   --force               Reinstall even when version matches
 *   --version             Print clawhub version
 *   --help                Show help
 */

import { createRequire } from 'node:module';
import { existsSync }    from 'node:fs';
import { readFile }      from 'node:fs/promises';
import path              from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Lazy imports (deferred so --version / --help work without network) ──────
const { cmdInstall }  = await import('./lib/installer.js');
const { cmdSearch }   = await import('./lib/registry.js');
const { cmdRollback, cmdVersions } = await import('./lib/versioning.js');
const { printHelp, red, yellow }   = await import('./lib/display.js');

// ── Package version ──────────────────────────────────────────────────────────
let PKG_VERSION = '1.0.0';
try {
  const pkg = JSON.parse(await readFile(path.join(__dirname, 'package.json'), 'utf8'));
  PKG_VERSION = pkg.version;
} catch {}

// ── Argument parsing ─────────────────────────────────────────────────────────
function parseArgs(argv) {
  const flags      = {};
  const positional = [];

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--skills-dir' && argv[i + 1]) { flags.skillsDir = argv[++i]; continue; }
    if (a === '--registry'   && argv[i + 1]) { flags.registry  = argv[++i]; continue; }
    if (a === '--dry-run')  { flags.dryRun = true;  continue; }
    if (a === '--force')    { flags.force  = true;  continue; }
    if (a === '--version' || a === '-v') { flags.version = true; continue; }
    if (a === '--help'    || a === '-h') { flags.help    = true; continue; }
    if (!a.startsWith('--')) positional.push(a);
  }

  return { flags, positional };
}

const { flags, positional } = parseArgs(process.argv.slice(2));

if (flags.version) {
  console.log(PKG_VERSION);
  process.exit(0);
}

if (flags.help || positional.length === 0) {
  printHelp(PKG_VERSION);
  process.exit(0);
}

// ── Skills directory resolution ───────────────────────────────────────────────
function resolveSkillsDir(override) {
  if (override) return path.resolve(override);

  // Walk up from cwd looking for a skills/ directory.
  let dir = process.cwd();
  for (let i = 0; i < 3; i++) {
    const candidate = path.join(dir, 'skills');
    if (existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  // Default: ./skills relative to cwd.
  return path.join(process.cwd(), 'skills');
}

// ── Build execution context ───────────────────────────────────────────────────
const ctx = {
  skillsDir:   resolveSkillsDir(flags.skillsDir),
  registryUrl: flags.registry ?? process.env.CLAWHUB_REGISTRY ?? 'https://clawhub.ai',
  dryRun:      flags.dryRun ?? false,
  force:       flags.force  ?? false,
};

// ── Dispatch ──────────────────────────────────────────────────────────────────
const [cmd, ...cmdArgs] = positional;

try {
  switch (cmd) {
    case 'install':
    case 'i':
      await cmdInstall(cmdArgs, ctx);
      break;

    case 'search':
    case 's':
      await cmdSearch(cmdArgs, ctx);
      break;

    case 'list':
    case 'ls': {
      const { cmdList } = await import('./lib/installer.js');
      await cmdList(ctx);
      break;
    }

    case 'rollback':
      await cmdRollback(cmdArgs, ctx);
      break;

    case 'versions':
      await cmdVersions(cmdArgs, ctx);
      break;

    default:
      console.error(red(`Unknown command: ${cmd}`));
      printHelp(PKG_VERSION);
      process.exit(1);
  }
} catch (err) {
  console.error(`\n${red('Error:')} ${err.message}\n`);
  process.exit(1);
}
