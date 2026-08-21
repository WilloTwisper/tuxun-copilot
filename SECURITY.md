# Security Policy

## Credentials

This project requires user-owned credentials. Never commit or publish:

- Google Gemini API keys (`API_KEY`, `API_KEY2`)
- Tuxun cookies (`fun_ticket`, `SESSION`, `_t_cfg`)
- Clash controller secrets (`CLASH_SECRET`)
- a populated `.env` file, terminal logs, or screenshots containing these values

Use `.env.example` only as a template. The real `.env` file is ignored by Git.

If a credential is exposed, revoke or rotate it immediately at the issuing service. Removing it from the latest commit is not sufficient because it may remain in Git history and external clones.

## Reporting

Do not open a public issue containing credentials or reproducible account data. Report security concerns privately through GitHub's security advisory feature for this repository.

## Scope

This project communicates with third-party services. Their availability, authentication behavior, and security policies are outside this repository's control. Keep dependencies current and review configuration changes before running the program.
