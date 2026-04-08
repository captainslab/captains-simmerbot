/**
 * clawhub/lib/display.js
 * Terminal output helpers: ANSI colors, table formatter, help text.
 */

const NO_COLOR =
  process.env.NO_COLOR !== undefined ||
  process.env.CI !== undefined ||
  !process.stdout.isTTY;

const ESC = '\x1b[';
const RESET = '\x1b[0m';

const esc = (code, s) => (NO_COLOR ? s : `${ESC}${code}m${s}${RESET}`);

export const bold   = s => esc('1',  s);
export const dim    = s => esc('2',  s);
export const red    = s => esc('31', s);
export const green  = s => esc('32', s);
export const yellow = s => esc('33', s);
export const cyan   = s => esc('36', s);

/**
 * Print the main help screen.
 * @param {string} version
 */
export function printHelp(version = '1.0.0') {
  console.log(`
${bold('clawhub')} v${version}  —  OpenClaw skill manager

${bold('USAGE')}
  clawhub <command> [options]

${bold('COMMANDS')}
  ${cyan('search')}  [query]               Search the skill registry
  ${cyan('install')} <slug>[@version]      Install or update a skill
  ${cyan('list')}                          List installed skills with versions
  ${cyan('rollback')} <slug> [@version]    Restore the previous (or named) version
  ${cyan('versions')} <slug>              Show full install history for a skill

${bold('OPTIONS')}
  ${dim('--skills-dir <path>')}   Skills directory  (default: ./skills)
  ${dim('--registry <url>')}      Registry base URL (default: https://clawhub.ai)
  ${dim('--dry-run')}             Preview without writing files
  ${dim('--force')}               Re-install even if version matches
  ${dim('--version')}             Show clawhub version
  ${dim('--help')}                Show this help

${bold('EXAMPLES')}
  clawhub install sonoscli
  clawhub install polymarket-fast-loop@1.5.0
  clawhub search weather
  clawhub list
  clawhub rollback polymarket-fast-loop
  clawhub versions polymarket-fast-loop

${bold('ENV VARS')}
  ${dim('CLAWHUB_REGISTRY')}      Override registry URL
  ${dim('GITHUB_TOKEN')}          GitHub PAT for higher API rate limits (60 → 5000 req/hr)
`);
}

/**
 * Print a simple aligned table.
 * @param {string[]} headers
 * @param {string[][]} rows
 */
export function printTable(headers, rows) {
  const widths = headers.map((h, i) =>
    Math.max(h.length, ...rows.map(r => (r[i] ?? '').length))
  );

  const line = headers.map((h, i) => bold(h.padEnd(widths[i]))).join('  ');
  const sep  = widths.map(w => '─'.repeat(w)).join('  ');

  console.log('  ' + line);
  console.log('  ' + sep);
  for (const row of rows) {
    const cells = row.map((cell, i) => {
      const s = (cell ?? '').padEnd(widths[i]);
      return i === 0 ? cyan(s) : dim(s);
    });
    console.log('  ' + cells.join('  '));
  }
}
