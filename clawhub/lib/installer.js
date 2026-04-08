/**
 * clawhub/lib/installer.js
 *
 * `install` and `list` command implementations.
 */

import { existsSync } from 'node:fs';
import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { bold, cyan, dim, green, red, yellow } from './display.js';
import { getSkillMeta, getSkillFiles } from './registry.js';
import { saveBackup } from './versioning.js';

// ---------------------------------------------------------------------------
// `clawhub install <slug>[@version]`
// ---------------------------------------------------------------------------

export async function cmdInstall(args, ctx) {
  const [slugVersion] = args;
  if (!slugVersion) {
    console.error('Usage: clawhub install <slug>[@version]');
    process.exit(1);
  }

  const atIdx = slugVersion.lastIndexOf('@');
  // '@' at position 0 means it's a scoped package name, not a version pin.
  const hasVersion = atIdx > 0;
  const slug            = hasVersion ? slugVersion.slice(0, atIdx) : slugVersion;
  const requestedVersion = hasVersion ? slugVersion.slice(atIdx + 1) : 'latest';

  const { skillsDir, registryUrl, dryRun, force } = ctx;
  const skillDir = path.join(skillsDir, slug);

  console.log(`\n📦  ${bold(`clawhub install ${slug}`)}${hasVersion ? `@${requestedVersion}` : ''}\n`);

  // ── 1. Fetch metadata ────────────────────────────────────────────────────
  process.stdout.write('  Fetching skill metadata... ');
  let meta;
  try {
    meta = await getSkillMeta(slug, requestedVersion, registryUrl);
  } catch (e) {
    console.log(red('✗'));
    console.error(red(`\n  Error: ${e.message}\n`));
    process.exit(1);
  }
  const version = meta.version && meta.version !== 'latest' ? meta.version : requestedVersion;
  console.log(`${green('✓')}  ${dim(slug)} ${cyan(`v${version}`)}`);

  // ── 2. Check if already installed ───────────────────────────────────────
  let currentVersion = null;
  const metaPath = path.join(skillDir, '_meta.json');
  if (existsSync(metaPath)) {
    try {
      currentVersion = JSON.parse(await readFile(metaPath, 'utf8')).version ?? null;
    } catch {}
  }
  if (!existsSync(skillDir)) {
    // Also check origin.json (more authoritative for install state).
  } else {
    const originPath = path.join(skillDir, '.clawhub', 'origin.json');
    if (existsSync(originPath)) {
      try {
        currentVersion = JSON.parse(await readFile(originPath, 'utf8')).installedVersion ?? currentVersion;
      } catch {}
    }
  }

  if (currentVersion && currentVersion === version && !force) {
    console.log(`\n  ${yellow('→')} ${bold(slug)} is already at ${cyan(`v${version}`)}.\n`);
    console.log(`  ${dim('Use --force to reinstall.')}\n`);
    return;
  }

  // ── 3. Resolve file list ─────────────────────────────────────────────────
  process.stdout.write('  Resolving files... ');
  let files;
  try {
    files = await getSkillFiles(slug, version, meta, registryUrl);
  } catch (e) {
    console.log(red('✗'));
    console.error(red(`\n  Error: ${e.message}\n`));
    process.exit(1);
  }
  console.log(`${green('✓')}  ${dim(`${files.length} file(s)`)}`);

  // ── 4. Backup current install ─────────────────────────────────────────────
  if (currentVersion && existsSync(skillDir) && !dryRun) {
    process.stdout.write(`  Backing up v${currentVersion}... `);
    try {
      await saveBackup(skillDir, currentVersion);
      console.log(green('✓'));
    } catch (e) {
      console.log(yellow('⚠ skipped'));
      console.error(`  ${dim(`(backup failed: ${e.message})`)}`);
    }
  }

  // ── 5. Download files ────────────────────────────────────────────────────
  console.log(`\n  ${dim('Downloading:')}`);
  if (!dryRun) await mkdir(skillDir, { recursive: true });

  for (const file of files) {
    if (dryRun) {
      console.log(`  ${dim('→')} ${file.path}`);
      continue;
    }
    const destPath = path.join(skillDir, file.path);
    try {
      await downloadFile(file.downloadUrl, destPath);
      console.log(`  ${green('✓')} ${file.path}`);
    } catch (e) {
      console.error(`  ${red('✗')} ${file.path}  ${dim(`(${e.message})`)}`);
    }
  }

  if (!dryRun) {
    // ── 6. Write .clawhub/origin.json ──────────────────────────────────────
    const clawhubDir = path.join(skillDir, '.clawhub');
    await mkdir(clawhubDir, { recursive: true });
    await writeFile(
      path.join(clawhubDir, 'origin.json'),
      JSON.stringify(
        {
          version: 1,
          registry: registryUrl,
          slug,
          installedVersion: version,
          installedAt: Date.now(),
        },
        null,
        2,
      ) + '\n',
    );

    // ── 7. Ensure _meta.json exists (create if missing from registry) ───────
    const skillMetaPath = path.join(skillDir, '_meta.json');
    if (!existsSync(skillMetaPath)) {
      await writeFile(
        skillMetaPath,
        JSON.stringify({ slug, version, publishedAt: Date.now() }, null, 2) + '\n',
      );
    }
  }

  // ── 8. Post-install summary ───────────────────────────────────────────────
  const clawhubJsonPath = path.join(skillDir, 'clawhub.json');
  if (!dryRun && existsSync(clawhubJsonPath)) {
    try {
      const manifest = JSON.parse(await readFile(clawhubJsonPath, 'utf8'));
      const envVars   = manifest.requires?.env  ?? [];
      const pipPkgs   = manifest.requires?.pip  ?? [];
      const entrypoint = manifest.automaton?.entrypoint;

      if (envVars.length > 0) {
        console.log(`\n  ${yellow('Required env vars:')}`);
        for (const e of envVars) console.log(`    ${dim('$')}${e}`);
      }
      if (pipPkgs.length > 0) {
        console.log(`\n  ${yellow('Install Python deps:')}`);
        console.log(`    pip install ${pipPkgs.join(' ')}`);
      }
      if (entrypoint) {
        console.log(`\n  ${yellow('Entrypoint:')} ${dim(`python ${entrypoint}`)}`);
      }
    } catch {}
  }

  const action = currentVersion
    ? `updated ${dim(`v${currentVersion} →`)} ${cyan(`v${version}`)}`
    : `installed ${cyan(`v${version}`)}`;
  console.log(`\n${green('✅')}  ${bold(slug)} ${action}${dryRun ? dim(' (dry run)') : ''}\n`);

  if (currentVersion && currentVersion !== version && !dryRun) {
    console.log(`  ${dim(`Previous v${currentVersion} saved → .clawhub/history/${currentVersion}/`)}`);
    console.log(`  ${dim(`To rollback: clawhub rollback ${slug}`)}\n`);
  }
}

// ---------------------------------------------------------------------------
// `clawhub list`
// ---------------------------------------------------------------------------

export async function cmdList(ctx) {
  const { skillsDir } = ctx;

  if (!existsSync(skillsDir)) {
    console.log(`\n  ${yellow('No skills directory found.')} (${skillsDir})\n`);
    return;
  }

  let entries;
  try {
    entries = await readdir(skillsDir, { withFileTypes: true });
  } catch {
    console.error(red(`Cannot read skills directory: ${skillsDir}`));
    process.exit(1);
  }

  const dirs = entries.filter(e => e.isDirectory());
  if (dirs.length === 0) {
    console.log(`\n  ${yellow('No skills installed.')}  (${skillsDir})\n`);
    return;
  }

  console.log(`\n📦  Installed skills  ${dim(skillsDir)}\n`);

  const rows = [];
  for (const dir of dirs) {
    const sd = path.join(skillsDir, dir.name);
    let version = '?', date = '', description = '';

    const originPath = path.join(sd, '.clawhub', 'origin.json');
    const metaPath   = path.join(sd, '_meta.json');

    if (existsSync(originPath)) {
      try {
        const origin = JSON.parse(await readFile(originPath, 'utf8'));
        version = origin.installedVersion ?? version;
        if (origin.installedAt) date = new Date(origin.installedAt).toISOString().slice(0, 10);
      } catch {}
    } else if (existsSync(metaPath)) {
      try {
        version = JSON.parse(await readFile(metaPath, 'utf8')).version ?? version;
      } catch {}
    }

    const skillMdPath = path.join(sd, 'SKILL.md');
    if (existsSync(skillMdPath)) {
      try {
        const content = await readFile(skillMdPath, 'utf8');
        const m = content.match(/^description:\s*(.+)$/m);
        if (m) description = m[1].replace(/^['"]|['"]$/g, '').slice(0, 55);
      } catch {}
    }

    rows.push([dir.name, `v${version}`, date, description]);
  }

  const { printTable } = await import('./display.js');
  printTable(['SKILL', 'VERSION', 'INSTALLED', 'DESCRIPTION'], rows);
  console.log(`\n  ${dim(`${rows.length} skill(s).  Rollback: clawhub rollback <slug>`)}\n`);
}

// ---------------------------------------------------------------------------
// File download
// ---------------------------------------------------------------------------

async function downloadFile(url, destPath) {
  await mkdir(path.dirname(destPath), { recursive: true });

  const resp = await fetch(url, {
    headers: { 'User-Agent': 'clawhub/1.0 (https://github.com/captainslab/captains-simmerbot)' },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

  const buf = Buffer.from(await resp.arrayBuffer());
  await writeFile(destPath, buf);
}
