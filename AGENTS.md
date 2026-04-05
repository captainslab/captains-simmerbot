# Codex Operating Rules for Simmer BTC Sprint Bot

This repo is for live-money trading infrastructure. Accuracy, verification, and controlled execution matter more than speed.

## Objective
Build and deploy a BTC 5m/15m Polymarket sprint bot on Simmer using:
- a fresh Simmer agent
- claim into the existing Simmer account
- the same wallet already attached to that account, if the docs and runtime confirm it is usable
- a single installable strategy skill
- Autoresearch enabled from day one

## Core rules
1. Use official Simmer docs as source of truth:
   - https://docs.simmer.markets/llms.txt
   - https://docs.simmer.markets/llms-full.txt
2. Do not invent endpoints, SDK methods, wallet behavior, or deployment steps.
3. Stop on any real documented blocker.
4. Default to dry-run unless explicitly executing live.
5. Never print full secrets.
6. Add env files to .gitignore.
7. No vague completion claims. Proof is required.
8. No unbounded brainstorming. One exploration round only.
9. Do not assume memory across agents. Write needed state to files.
10. Keep the initial agent team minimal.

## Active daily-driver skill stack
Use the installed skill roots under `~/.codex/skills/` for:
- `execution-prompt-optimizer`
- `aight-relay-controller`
- `superpowers-runtime`
- `agent-deployer`
- `memory-optimizer`
- `persistent-self-improvement`
- `auto-learning-research`
- `auto-skill-learning`

Legacy daily-driver skills remain installed on disk but are not part of the foregrounded active stack.

## Auto-trigger policy
The stack is auto-selected. Do not ask the user to choose among these skills manually.

### Precedence
1. `execution-prompt-optimizer`
   - Trigger on rough, vague, incomplete, messy, or contradictory input.
   - Use only to normalize the request before any execution or relay step.
2. `aight-relay-controller`
   - Trigger on team-lead relay tasks, pasted agent/team output, or "what do I send next" workflows.
   - Use only to turn upstream output into the next message.
3. `superpowers-runtime`
   - Trigger by default once execution begins after routing is complete.
   - Use to enforce execution posture and guardrails.

### Conditional skills
When more than one conditional trigger is plausible, choose the first skill below whose trigger conditions are met.

4. `agent-deployer`
   - Trigger only for capability gaps, independent verification, repeated deployment failure, or an isolated deployment branch that needs deploy/rollback/health-check handling.
5. `memory-optimizer`
   - Trigger only for checkpointing, interruption, resume, or stale-state cleanup.
6. `persistent-self-improvement`
   - Trigger only when the same non-deployment correction or failure has repeated and the fix should become a reusable local pattern.
7. `auto-learning-research`
   - Trigger only for research-heavy tasks, source synthesis, comparison work, or durable lesson capture grounded in external evidence.
8. `auto-skill-learning`
   - Trigger only when a workflow repeats enough to formalize into a new skill or skill update, not when a one-off fix or research note is enough.

### Conflict rules
- Map each request to one primary skill at a time.
- `execution-prompt-optimizer` and `aight-relay-controller` are input-routing skills only.
- `superpowers-runtime` is the execution default after routing and does not compete with the more specific conditionals.
- The conditional skills are mutually exclusive by intent; if multiple seem possible, use the first one in the precedence list above.
- Keep old skills installed but not foregrounded unless a trigger explicitly selects them.

### Examples
- "Turn this messy note into a plan" -> `execution-prompt-optimizer`
- "What do I send the team from this update?" -> `aight-relay-controller`
- "Run the approved task now" -> `superpowers-runtime`
- "Deploy the updated agent and verify health" -> `agent-deployer`
- "Resume after interruption and restore context" -> `memory-optimizer`
- "We keep making the same correction; capture the pattern" -> `persistent-self-improvement`
- "Compare these sources and synthesize the durable lesson" -> `auto-learning-research`
- "This workflow has repeated enough to become a skill" -> `auto-skill-learning`

## Initial agent policy
Deploy only these initial agents:
- team-lead
- product-manager
- trading-bot-engineer

Do not deploy more agents unless blocked or independent verification is needed.

## First-message rule
The first agent message must start with @team-lead and run one structured exploration round only.

All responders must answer in numbered format only:
1. What is the real problem?
2. What does success look like in measurable terms?
3. What is the most likely failure?
4. Do we need to deploy another agent now or later?
5. If yes:
   - exact role
   - exact reason
   - exact task to isolate
   - exact proof expected back
6. If no, why current coverage is sufficient

After all responses:
@product-manager must converge and return only:
1. MVP
2. Scope IN
3. Scope OUT
4. Risks
5. Whether to deploy another agent
6. Single next execution step

No second exploration round.

## Repository outputs required
Create and maintain at minimum:
- AGENTS.md
- .gitignore
- .env.example
- README.md
- skills/btc-sprint-stack/SKILL.md
- skills/btc-sprint-stack/clawhub.json
- skills/btc-sprint-stack/main.py
- skills/btc-sprint-stack/config/defaults.json
- skills/btc-sprint-stack/modules/btc_sprint_signal.py
- skills/btc-sprint-stack/modules/btc_regime_filter.py
- skills/btc-sprint-stack/modules/btc_sprint_executor.py
- skills/btc-sprint-stack/modules/btc_position_manager.py
- skills/btc-sprint-stack/modules/btc_trade_journal.py
- skills/btc-sprint-stack/modules/btc_self_learn.py
- skills/btc-sprint-stack/modules/btc_heartbeat.py
- skills/btc-sprint-stack/scripts/analyze_sprints.py
- skills/btc-sprint-stack/data/.gitkeep

## Skill architecture rule
Do not deploy seven separate live strategy skills on a $60 bankroll.
Build one installable primary skill:
- btc-sprint-stack

Inside that skill, implement these modules:
1. btc-sprint-signal
2. btc-regime-filter
3. btc-sprint-executor
4. btc-position-manager
5. btc-trade-journal
6. btc-self-learn
7. btc-heartbeat

Install that one skill into the agent.
Install simmer-autoresearch separately as a plugin.

## Wallet rule
Assume managed-wallet reuse first because the account already has a wallet attached.
Do not request WALLET_PRIVATE_KEY unless the docs or runtime prove managed-wallet reuse is impossible for the fresh claimed agent.
If impossible, stop and report:
- exact blocker
- exact evidence
- exact human action needed

## Required risk defaults
- bankroll_usd = 60
- max_trade_usd = 4
- max_daily_loss_usd = 10
- max_open_positions = 2
- max_single_market_exposure_usd = 8
- max_trades_per_day = 6
- min_edge = 0.07
- min_confidence = 0.65
- max_slippage_pct = 0.10
- stop_loss_pct = 0.10
- take_profit_pct = 0.12
- cooldown_after_loss_minutes = 60
- cycle_interval_minutes = 15

## Strategy requirements
Target BTC 5m/15m Polymarket sprint markets.
The primary skill must include:
- signal engine
- regime filter
- fee-aware executor
- bankroll-aware position manager
- trade journal
- self-learning rule engine
- heartbeat orchestrator

Every trade must include:
- source
- skill_slug
- reasoning
- signal_data.edge
- signal_data.confidence
- signal_data.signal_source

Use flat signal_data only.

## Autoresearch requirements
Install and configure simmer-autoresearch.
- maxExperiments = 12
- target skill slug = btc-sprint-stack
- backtest before live mutation whenever possible
- only tune:
  - min_edge
  - min_confidence
  - max_slippage_pct
  - cycle_interval_minutes
  - stop_loss_pct
  - take_profit_pct

## Verification requirements
Nothing is done without proof.
Before reporting complete, show:
1. agent created
2. API key saved and masked
3. claim URL surfaced
4. claimed status confirmed
5. wallet mode actually confirmed
6. skill built
7. skill installed on agent
8. plugin installed and active
9. dry-run command
10. live command
11. stop command
12. logs path
13. remaining manual steps

## Human checkpoint
You must pause only for:
- the Simmer claim step
- a real documented blocker

Everything else should be executed end to end.
