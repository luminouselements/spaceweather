# Security Policy

## Scope

This repository contains a Python script that retrieves public NOAA Space
Weather Prediction Center (SWPC) alerts and sends selected messages to a
Meshtastic node over TCP.

## Reporting a vulnerability

Please do not report suspected vulnerabilities in a public issue. Use GitHub's
**Report a vulnerability** feature for this repository if private vulnerability
reporting is enabled. Otherwise, contact the repository maintainer privately
before sharing details publicly.

Include the affected file or version, steps to reproduce, potential impact, and
any relevant logs with secrets removed. Do not include `.env` contents,
credentials, private channel keys, or other sensitive information in a report.

## Operational security

- Keep `.env`, node credentials, and Meshtastic private channel settings out of
  version control. The example environment file contains non-secret defaults;
  replace them locally as needed.
- Restrict access to the Meshtastic TCP interface to trusted hosts and networks.
  Do not expose it directly to the public internet.
- Run the script with only the filesystem and network permissions it needs.
- Keep Python and installed dependencies current; review dependency changes
  before deploying updates.
- Treat SWPC feed content and network responses as external input. The script
  formats feed text for a short radio broadcast; it does not authenticate or
  encrypt the Meshtastic TCP connection.
- Review logs before sharing them, since runtime details may reveal host,
  network, or deployment information.

## Support

Security fixes are made in the repository's default branch. No separate
long-term-support branches or response-time guarantees are currently defined.
