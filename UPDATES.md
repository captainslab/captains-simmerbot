# Updates

Use this when you want to pull the latest repo changes onto a machine that already has the bot installed.

## Standard update flow

```bash
cd captains-simmerbot
git pull origin master
source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q
```

## Restart after the update

If the bot is running in the background, restart it after the update:

```bash
bin/start_btc_bot.sh
```

## If behavior changed

- If you changed dependencies, reinstall them.
- If you changed Discord control paths, test `?` and `!help` in Discord.
- If you changed trading logic, run the dry-run command before going live.

## Safe release checklist

1. Run the test suite.
2. Review the diff.
3. Confirm no secrets were added.
4. Push to GitHub.
5. Restart the live loop on the target machine.
