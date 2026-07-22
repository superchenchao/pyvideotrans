# Security

- Never place DeepSeek, Azure, Hugging Face, OSS or internal Worker credentials in job JSON, logs or Git.
- Use a dedicated secret manager and short-lived object URLs.
- Internal Worker endpoints must require service authentication and private networking.
- The sample bearer token is only an interface; production should use workload identity or mTLS where available.
- Validate returned artifacts before publishing signed download URLs.
- Rotate any credential that has appeared in a terminal transcript, issue, test fixture or commit.
