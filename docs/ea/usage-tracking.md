## Usage tracking

The bot appends claude.ai plan usage (session % and weekly %) to every
response footer. The user can also type "usage" or "quota" in chat for
a detailed report (zero tokens consumed, handled by the bot directly).
Usage data comes from the Anthropic OAuth API using Claude Code's own
credentials at `~/.claude/.credentials.json` of the account the EA runs
on: the bot (the owner) runs `claude_usage.py` as the resident user (ea-bot)
and reports that login's numbers, named on the report's first line. The
owner's own Claude login is a different account with its own quota.
