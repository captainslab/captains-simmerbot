/**
 * clawhub/lib/registry.js
 *
 * Registry client: searches clawhub.ai and fetches skill file lists.
 * Falls back to the public SpartanLabsXyz/simmer-sdk GitHub repo for
 * skills that ship with the Simmer SDK.
 */

const GITHUB_API   = 'https://api.github.com';
const GITHUB_ORG   = 'SpartanLabsXyz';
const GITHUB_REPO  = 'simmer-sdk';
const SKILLS_PATH  = 'skills';

// GitHub headers — include token if available to avoid 60 req/hr limit.
const ghHeaders = () => {
  const h = {
    'User-Agent': 'clawhub/1.0 (https://github.com/captainslab/captains-simmerbot)',
    'Accept': 'application/vnd.github.v3+json',
  };
  if (process.env.GITHUB_TOKEN) h['Authorization'] = `Bearer ${process.env.GITHUB_TOKEN}`;
  return h;
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Search the registry for skills matching `query`.
 * Tries the clawhub.ai API first, then falls back to GitHub.
 *
 * @param {string} query
 * @param {string} registryUrl
 * @param {object} opts
 * @returns {Promise<Array<{slug, name, description, version, author}>>}
 */
export async function searchSkills(query = '', registryUrl = 'https://clawhub.ai', { limit = 30 } = {}) {
  // 1. Registry API
  try {
    const url = `${registryUrl}/api/v1/skills?q=${encodeURIComponent(query)}&limit=${limit}`;
    const data = await fetchJSON(url, { timeout: 5000 });
    const skills = data.skills ?? data.results ?? data.data ?? [];
    if (Array.isArray(skills) && skills.length > 0) return skills;
  } catch {
    // fall through to GitHub
  }

  // 2. GitHub fallback
  return searchViaGitHub(query, limit);
}

/**
 * Fetch metadata for a single skill (including resolved version).
 * Returns a plain object with at least `{ slug, version }`.
 *
 * @param {string} slug
 * @param {string} version  'latest' or semver string
 * @param {string} registryUrl
 */
export async function getSkillMeta(slug, version = 'latest', registryUrl = 'https://clawhub.ai') {
  // 1. Registry API
  try {
    const suffix = version !== 'latest' ? `/versions/${encodeURIComponent(version)}` : '';
    const url = `${registryUrl}/api/v1/skills/${encodeURIComponent(slug)}${suffix}`;
    const meta = await fetchJSON(url, { timeout: 8000 });
    if (meta?.slug || meta?.name) return meta;
  } catch {
    // fall through
  }

  // 2. GitHub fallback
  return getSkillMetaFromGitHub(slug, version);
}

/**
 * Get the list of files to download for a skill version.
 * Returns `Array<{ path: string, downloadUrl: string }>` where `path` is
 * relative to the skill root directory.
 *
 * @param {string} slug
 * @param {string} version
 * @param {object|null} meta   Result from getSkillMeta (may include file list)
 * @param {string} registryUrl
 */
export async function getSkillFiles(slug, version = 'latest', meta = null, registryUrl = 'https://clawhub.ai') {
  // If the registry already embedded a file list, use it.
  if (Array.isArray(meta?.files) && meta.files.length > 0) return meta.files;

  // GitHub fallback
  return getSkillFilesFromGitHub(slug);
}

// ---------------------------------------------------------------------------
// CLI command handler
// ---------------------------------------------------------------------------

/**
 * `clawhub search [query]`
 */
export async function cmdSearch(args, ctx) {
  const { bold, cyan, dim, green, yellow } = await import('./display.js');
  const query = args.join(' ').trim();

  console.log(`\n🔍 Searching${query ? ` for ${bold(query)}` : ' all skills'}...\n`);

  let results;
  try {
    results = await searchSkills(query, ctx.registryUrl, { limit: 40 });
  } catch (e) {
    console.error(`  ❌ Search failed: ${e.message}`);
    process.exit(1);
  }

  if (results.length === 0) {
    console.log(`  ${yellow('No skills found.')}\n`);
    return;
  }

  const rows = results.map(s => [
    s.slug || s.name || '',
    s.version ? `v${s.version}` : '',
    s.description ? s.description.slice(0, 70) + (s.description.length > 70 ? '…' : '') : '',
  ]);

  const { printTable } = await import('./display.js');
  printTable(['SKILL', 'VERSION', 'DESCRIPTION'], rows);

  console.log(`\n  ${dim(`${results.length} result(s).  Install with: clawhub install <slug>`)}\n`);
}

// ---------------------------------------------------------------------------
// GitHub fallback implementations
// ---------------------------------------------------------------------------

async function searchViaGitHub(query, limit) {
  let dirs;
  try {
    const url = `${GITHUB_API}/repos/${GITHUB_ORG}/${GITHUB_REPO}/contents/${SKILLS_PATH}`;
    const entries = await fetchJSON(url, { headers: ghHeaders(), timeout: 8000 });
    dirs = entries.filter(e => e.type === 'dir');
  } catch (e) {
    throw new Error(`Registry unreachable and GitHub fallback also failed: ${e.message}`);
  }

  const results = [];
  for (const dir of dirs) {
    if (results.length >= limit) break;
    try {
      const mdUrl = `${GITHUB_API}/repos/${GITHUB_ORG}/${GITHUB_REPO}/contents/${SKILLS_PATH}/${dir.name}/SKILL.md`;
      const mdMeta = await fetchJSON(mdUrl, { headers: ghHeaders(), timeout: 6000 });
      const content = Buffer.from(mdMeta.content, 'base64').toString('utf8');

      const description = frontmatterField(content, 'description');
      const version     = frontmatterField(content, 'version')?.replace(/"/g, '');
      const displayName = frontmatterField(content, 'displayName') || dir.name;

      const q = query.toLowerCase();
      const matches = !query
        || dir.name.toLowerCase().includes(q)
        || (description && description.toLowerCase().includes(q))
        || (displayName && displayName.toLowerCase().includes(q));

      if (matches) {
        results.push({ slug: dir.name, name: displayName, description, version, source: 'github' });
      }
    } catch {
      // Skip skills without SKILL.md or unreadable metadata
    }
  }
  return results;
}

async function getSkillMetaFromGitHub(slug, version) {
  const files = await getSkillFilesFromGitHub(slug);

  // Try to read version from _meta.json if available
  let resolvedVersion = version === 'latest' ? undefined : version;
  const metaFile = files.find(f => f.path === '_meta.json');
  if (metaFile && !resolvedVersion) {
    try {
      const text = await fetchText(metaFile.downloadUrl);
      const obj = JSON.parse(text);
      resolvedVersion = obj.version;
    } catch {}
  }

  return { slug, version: resolvedVersion ?? version, source: 'github', files };
}

async function getSkillFilesFromGitHub(slug) {
  const files = [];
  try {
    await traverseGitHubDir(`${SKILLS_PATH}/${slug}`, '', files);
  } catch (e) {
    if (e.status === 404) {
      throw new Error(`Skill "${slug}" not found in GitHub registry (${GITHUB_ORG}/${GITHUB_REPO}). Is it a custom skill on clawhub.ai?`);
    }
    throw e;
  }
  if (files.length === 0) {
    throw new Error(`Skill "${slug}" returned no files from registry.`);
  }
  return files;
}

async function traverseGitHubDir(ghPath, relPrefix, files) {
  const url = `${GITHUB_API}/repos/${GITHUB_ORG}/${GITHUB_REPO}/contents/${ghPath}`;
  const entries = await fetchJSON(url, { headers: ghHeaders(), timeout: 8000 });

  for (const entry of entries) {
    const relPath = relPrefix ? `${relPrefix}/${entry.name}` : entry.name;

    if (entry.type === 'file') {
      // Skip .clawhub/ — we generate origin.json ourselves
      if (relPath.startsWith('.clawhub/')) continue;
      files.push({ path: relPath, downloadUrl: entry.download_url });
    } else if (entry.type === 'dir') {
      if (entry.name === '.clawhub') continue;
      await traverseGitHubDir(`${ghPath}/${entry.name}`, relPath, files);
    }
  }
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function fetchJSON(url, { headers = {}, timeout = 10000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  let resp;
  try {
    resp = await fetch(url, {
      headers: { Accept: 'application/json', ...headers },
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }

  if (!resp.ok) {
    const err = new Error(`HTTP ${resp.status} fetching ${url}`);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

async function fetchText(url) {
  const resp = await fetch(url, {
    headers: { 'User-Agent': 'clawhub/1.0' },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${url}`);
  return resp.text();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Extract a field value from SKILL.md YAML frontmatter. */
function frontmatterField(content, field) {
  // Handles both top-level and indented (metadata block) fields
  const pattern = new RegExp(`^\\s*${field}:\\s*(.+)$`, 'm');
  const m = content.match(pattern);
  if (!m) return '';
  return m[1].trim().replace(/^['"]|['"]$/g, '');
}
