# Certificates

`rds-global-bundle.pem`: Amazon RDS's public CA bundle (every region's root and
intermediate CAs), so the gateway can verify Aurora's TLS certificate
(`GATEWAY_DB_SSL=verify-full`, the default on AWS). Kept in the repository rather than
downloaded at build time, so builds need no network access to AWS and the file only
changes on purpose.

- Source: <https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem>
- Fetched 2026-10-17: 114 certificates, the first expiring 2061-05-18.
- SHA-256: `90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b`

To refresh it, download it again from the source, then check that it holds only
certificates and record the new checksum here.
