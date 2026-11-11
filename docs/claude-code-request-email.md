# Request: Claude (with Claude Code) for network automation work

Draft email to Tim (supervisor) and Brendan (manager). Pricing checked 11 Nov 2026.

---

**Subject:** Request: Claude (with Claude Code) for network automation work

Hi Tim and Brendan,

I'd like to ask for approval to use a Claude subscription, which includes Claude Code, for my network automation work. Copilot is currently our only approved AI tool, so I'm asking for Claude to be assessed and approved for my role, either as a policy exception or through our normal approval process.

**Why**

A lot of my role is writing and maintaining Ansible playbooks, AWX jobs and NetBox integrations. Our current Copilot and Copilot Chat are good for general questions and documents, but they don't include an AI coding tool. That means they can't work across a whole playbook or repo, run checks, or help me test changes. Claude Code is built for exactly that kind of work.

As an example, I recently used it to build an Ansible playbook that syncs our firewalls from Strata Cloud Manager into NetBox. It handles the rule that only the active member of an HA pair holds the IPs, and it's ready to run in AWX. Writing that by hand would have taken me several days. With Claude Code it took a fraction of that, and I reviewed and tested it before it went anywhere near production.

**Cost**

- Claude Pro is about **A$340 a year** (A$28/month on annual billing, GST included). Claude Code is included, so there's no separate developer licence.
- If I end up using it heavily every day, the next tier is Max at A$170/month. I'd start on Pro and only move up if usage justifies it.
- GitHub Copilot isn't a cheap alternative for us. We use Gitea, not GitHub, so we'd need GitHub licensing first. GitHub Enterprise is US$21 per user per month, and the Copilot seat comes on top of that (Business US$19, Enterprise US$39), plus metered charges for agent use. That's roughly US$480 to US$720 a year per user before usage, compared with about A$340 for Claude Pro.

**Controls I'd put in place**

- No sensitive, personal, classified or customer data goes into Claude. That includes no credentials, secrets or live config containing them. I'd use sanitised or sample data instead.
- Everything Claude produces is reviewed and tested by me (check mode or lab first) before it touches production, and our normal change process still applies.
- It would be a council-approved account, with model training on our data switched off.
- Code stays in our own repos (Gitea), not public ones.
- I'm happy to document how it's used, so it lines up with the ISM and ACSC guidance on AI, and to work through any assessment IT security or governance need.

I'd suggest a 3 to 6 month trial on a single Pro licence, after which I can report back on time saved and any issues.

Happy to talk it through or give a quick demo.

Thanks,
Krystin

---

## Notes (not part of the email)

- The original "$10 extra per user for GitHub Enterprise" figure was inaccurate. GitHub Enterprise is US$21/user/month; US$10 is Copilot Pro (individual) or the new Copilot Standalone seat, both with metered agent usage on top.
- Individual Pro accounts let you turn model training off in settings. For training off by default, central billing and SSO, use the Team plan (A$34/seat/month annual, minimum 2 seats).
- Copilot Pro (US$10) can now run Claude Code as a third-party agent, but still needs GitHub and metered credits.

Sources: https://claude.com/pricing, https://sandlabs.com.au/blog/claude-code-pricing, https://github.com/pricing, https://github.com/features/copilot/plans
