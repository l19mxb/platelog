"""Validate a TLS key pair, hostname, chain and dates without opening a socket."""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path


def validate(certificate: Path, private_key: Path, server_name: str, trust_ca: Path | None = None) -> dict:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # An empty supplied password prevents OpenSSL from prompting for an encrypted key.
    server_context.load_cert_chain(certificate, private_key, password=b"")
    client_context = ssl.create_default_context()
    client_context.hostname_checks_common_name = False
    if trust_ca is not None:
        client_context.load_verify_locations(cafile=str(trust_ca))
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=server_name)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    complete = [False, False]
    for _ in range(100):
        for index, connection in enumerate((client, server)):
            if not complete[index]:
                try:
                    connection.do_handshake()
                    complete[index] = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
        if data := client_out.read():
            server_in.write(data)
        if data := server_out.read():
            client_in.write(data)
        if all(complete):
            peer = client.getpeercert()
            utc = lambda text: datetime.fromtimestamp(ssl.cert_time_to_seconds(text), timezone.utc).isoformat()
            return {"result": "Passed", "server_name": server_name, "tls": client.version(),
                    "not_before_utc": utc(peer["notBefore"]), "expires_utc": utc(peer["notAfter"]),
                    "certificate_sha256": hashlib.sha256(client.getpeercert(binary_form=True)).hexdigest(),
                    "names": [value for kind, value in peer.get("subjectAltName", ()) if kind in {"DNS", "IP Address"}],
                    "validation": "Key pair, hostname, certificate chain, Server Authentication and validity dates",
                    "trust_store_changed": False, "network_connections": 0}
    raise ValueError("TLS handshake did not complete")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--certificate", type=Path, required=True)
    cli.add_argument("--private-key", type=Path, required=True)
    cli.add_argument("--server-name", required=True)
    cli.add_argument("--trust-ca", type=Path)
    args = cli.parse_args()
    try:
        result = validate(args.certificate, args.private_key, args.server_name, args.trust_ca)
    except (OSError, ValueError) as error:
        # No key contents, credentials or platform exception messages in setup output.
        info = {"result": "Failed", "error_type": type(error).__name__,
                "reason": "Check the PEM key pair, SAN name, certificate dates and issuing CA trust."}
        if isinstance(error, ssl.SSLCertVerificationError):
            info["verify_code"] = error.verify_code
            info["reason"] = "Certificate verification failed. Check SAN, validity, Server Authentication and issuing CA trust."
        print(json.dumps(info), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
