/**
 * clawhub/lib/versioning.js
 *
 * Version history: backup-before-update, rollback, and history display.
 *
 * Layout inside each skill directory:
 *
 *   skills/<slug>/
 *   └── .clawhub/
 *       ├── origin.json              ← current install receipt
 *       ├── history.json             ← manifest of all past versions
 *       └── history/
 *           └── <version>/           ← snapshot taken before it was replaced
 *               ├── SKILL.md
 *               ├── clawhub.json
 *               ├── _meta.json
 *               ├── <script>.py
 *               └── _clawhub.origin.json  ← copy of origin.json for that version
 */

import { existsSync } from 'node:fs';
import { copyFile, cp, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { bold, cyan, dim, green, red, yellow } from './display.js';

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Back up the current install before overwriting it.
 * Saves all skill files (excluding .clawhub/history/) to
 * `.clawhub/history/<currentVersion>/` and appends an entry to history.json.
 *
 * @param {string} skillDir   Absolute path to the skill directory
 * @param {string} currentVersion   e.g. "1.4.0"
 */
export async function saveBackup(skillDir, currentVersion) {
  const backupDir = path.join(skillDir, '.clawhub', 'history', currentVersion);
  await mkdir(backupDir, { recursive: true });

  // Copy all skill files, skipping the .clawhub/ directory to avoid recursion.
  await copyTreeExcluding(skillDir, backupDir, n => n === '.clawhub');

  // Save a copy of origin.json (lives inside .clawhub/) alongside the backup.
  const originSrc = path.join(skillDir, '.clawhub', 'origin.json');
  if (existsSync(originSrc)) {
    await copyFile(originSrc, path.join(backupDir, '_clawhub.origin.json'));
  }

  // Append to history manifest.
  const history = await loadHistory(skillDir);
  // Remove any stale entry for the same version before appending.
  history.entries = history.entries.filter(e => e.version !== currentVersion);
  history.entries.push({ version: currentVersion, replacedAt: Date.now() });
  await saveHistory(skillDir, history);
}

/**
 * Restore a previous version from its backup.
 * Used by cmdRollback.
 *
 * @param {string} skillDir
 * @param {string} targetVersion  Version to restore
 */
export async function restoreBackup(skillDir, targetVersion) {
  const backupDir = path.join(skillDir, '.clawhub', 'history', targetVersion);
  if (!existsSync(backupDir)) {
    throw new Error(`No backup found for version ${targetVersion} (expected: ${backupDir})`);
  }

  // Restore all files from the backup, skipping the special origin copy.
  await copyTreeExcluding(backupDir, skillDir, n => n === '_clawhub.origin.json');

  // Restore origin.json.
  const originBackup = path.join(backupDir, '_clawhub.origin.json');
  if (existsSync(originBackup)) {
    await copyFile(originBackup, path.join(skillDir, '.clawhub', 'origin.json'));
  }
}

/**
 * Read `.clawhub/history.json` for a skill.
 * Returns `{ entries: [] }` if the file does not exist.
 */
export async function loadHistory(skillDir) {
  const histPath = path.join(skillDir, '.clawhub', 'history.json');
  try {
    return JSON.parse(await readFile(histPath, 'utf8'));
  } catch {
    return { entries: [] };
  }
}

/**
 * Write `.clawhub/history.json`.
 */
export async function saveHistory(skillDir, history) {
  const histPath = path.join(skillDir, '.clawhub', 'history.json');
  await mkdir(path.dirname(histPath), { recursive: true });
  await writeFile(histPath, JSON.stringify(history, null, 2) + '\n');
}

// ---------------------------------------------------------------------------
// CLI command handlers
// ---------------------------------------------------------------------------

/**
 * `clawhub rollback <slug> [@version]`
 * Restores the most-recent backup, or a specific one if named.
 */
export async function cmdRollback(args, ctx) {
  const [slug, namedVersion] = args;
  if (!slug) {
    console.error('Usage: clawhub rollback <slug> [@version]');
    process.exit(1);
  }

  const skillDir = path.join(ctx.skillsDir, slug);
  if (!existsSync(skillDir)) {
    console.error(red(`Skill "${slug}" is not installed.`));
    process.exit(1);
  }

  // Determine what version is currently installed.
  let currentVersion = 'unknown';
  try {
    const origin = JSON.parse(
      await readFile(path.join(skillDir, '.clawhub', 'origin.json'), 'utf8'),
    );
    currentVersion = origin.installedVersion ?? currentVersion;
  } catch {}

  // Pick the target version.
  const history = await loadHistory(skillDir);
  const backups  = history.entries.filter(e => !e.rollback);

  if (backups.length === 0) {
    console.error(yellow(`No backups found for "${slug}". Run clawhub install first to create a version history.`));
    process.exit(1);
  }

  let target;
  if (namedVersion) {
    const v = namedVersion.replace(/^@/, '');
    target = backups.find(e => e.version === v);
    if (!target) {
      console.error(red(`Version ${v} not found in backup history for "${slug}".`));
      console.error(`  Available: ${backups.map(e => e.version).join(', ')}`);
      process.exit(1);
    }
  } else {
    // Most recent backup.
    target = backups[backups.length - 1];
  }

  console.log(`\n⏪  Rolling back ${bold(slug)}: ${cyan(currentVersion)} → ${green(target.version)}\n`);

  if (ctx.dryRun) {
    console.log(`  ${dim('[dry-run]')} Would restore from .clawhub/history/${target.version}/\n`);
    return;
  }

  await restoreBackup(skillDir, target.version);

  // Record the rollback in history.
  history.entries.push({
    version: `${target.version}↩`,
    replacedAt: Date.now(),
    rollback: true,
    restoredFrom: target.version,
    priorVersion: currentVersion,
  });
  await saveHistory(skillDir, history);

  console.log(`${green('✅')} ${bold(slug)} restored to v${target.version}\n`);
  console.log(`  ${dim('To reinstall the latest: clawhub install ' + slug)}\n`);
}

/**
 * `clawhub versions <slug>`
 * Shows version history for an installed skill.
 */
export async function cmdVersions(args, ctx) {
  const [slug] = args;
  if (!slug) {
    console.error('Usage: clawhub versions <slug>');
    process.exit(1);
  }

  const skillDir = path.join(ctx.skillsDir, slug);
  if (!existsSync(skillDir)) {
    console.error(red(`Skill "${slug}" is not installed.`));
    process.exit(1);
  }

  let currentVersion = '?';
  try {
    const origin = JSON.parse(
      await readFile(path.join(skillDir, '.clawhub', 'origin.json'), 'utf8'),
    );
    currentVersion = origin.installedVersion ?? currentVersion;
  } catch {}

  console.log(`\n📋  Version history: ${bold(slug)}\n`);
  console.log(`  ${dim('Current:')} ${green(bold(currentVersion))}\n`);

  const history = await loadHistory(skillDir);

  if (history.entries.length === 0) {
    console.log(`  ${dim('No version history recorded yet.')}\n`);
    console.log(`  ${dim('History is created automatically when you upgrade a skill.')}\n`);
    return;
  }

  const rows = [...history.entries]
    .reverse()
    .map(e => {
      const date = new Date(e.replacedAt).toISOString().slice(0, 16).replace('T', ' ');
      const tag  = e.rollback ? yellow('  ↩ rollback') : '';
      return [date, `v${e.version}`, tag];
    });

  const { printTable } = await import('./display.js');
  printTable(['REPLACED AT (UTC)', 'VERSION', ''], rows);
  console.log();
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Recursively copy `srcDir` → `destDir`, skipping top-level entries whose
 * name matches `excludeTopLevel(name) === true`.
 *
 * Sub-directories are copied in full (no further exclusion).
 */
async function copyTreeExcluding(srcDir, destDir, excludeTopLevel) {
  await mkdir(destDir, { recursive: true });
  const entries = await readdir(srcDir, { withFileTypes: true });

  for (const ent of entries) {
    if (excludeTopLevel(ent.name)) continue;

    const src = path.join(srcDir, ent.name);
    const dst = path.join(destDir, ent.name);

    if (ent.isDirectory()) {
      await cp(src, dst, { recursive: true });
    } else {
      await copyFile(src, dst);
    }
  }
}
