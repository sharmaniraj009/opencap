"""PKI folders and client certificate checks for the northbound OPC UA server (D-068).

    <pki_dir>/own/cert.der           server certificate (generated self-signed on first start)
    <pki_dir>/own/private/key.pem    its private key (mode 0600)
    <pki_dir>/trusted/               trusted client certificates and CA certificates (DER or PEM)
    <pki_dir>/issuers/               intermediate CA certificates: used to build a chain to a
                                     trusted CA, never trusted themselves
    <pki_dir>/crl/                   certificate revocation lists of those CAs
    <pki_dir>/rejected/              client certificates that were refused, saved for an admin
                                     to inspect and move to trusted/ (as KEPServerEX and the
                                     OPC Foundation stacks do)
    <pki_dir>/users/                 certificates accepted as X.509 user identity tokens

The folders are read again on every CreateSession, so trusting a client needs no restart.
A client certificate passes when it is in its validity period, its URI matches the client's
ApplicationUri, its key usage allows a client (asyncua's TRUSTED_VALIDATION checks), and it is
trusted directly or through a CA in trusted/ (with intermediates from issuers/), and not revoked.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from asyncua import ua
from asyncua.common.utils import ServiceError
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography import x509
from cryptography.hazmat.primitives import serialization

log = structlog.get_logger("opencap.north.opcua_server")

PKI_FOLDERS = ("own/private", "trusted", "issuers", "crl", "rejected", "users")


def thumbprint(der: bytes) -> str:
    """SHA-1 thumbprint (hex, upper case): how OPC UA and KEPServerEX name certificates."""
    return hashlib.sha1(der, usedforsecurity=False).hexdigest().upper()


def common_name(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else ""


def load_certificates(folder: Path) -> list[x509.Certificate]:
    """Every DER or PEM certificate in `folder` (unreadable files are skipped and logged)."""
    certs: list[x509.Certificate] = []
    if not folder.is_dir():
        return certs
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        data = path.read_bytes()
        try:
            if b"-----BEGIN CERTIFICATE-----" in data:
                certs.extend(x509.load_pem_x509_certificates(data))
            else:
                certs.append(x509.load_der_x509_certificate(data))
        except ValueError:
            log.warning("opcua_server.certificate_unreadable", path=str(path))
    return certs


class _ChainTrustStore(TrustStore):
    """asyncua's TrustStore, with untrusted intermediates (issuers/) to build the chain."""

    def __init__(self, trusted: list[Path], crls: list[Path], issuers: list[x509.Certificate]) -> None:
        super().__init__(trusted, crls)
        self._issuers = issuers

    def is_trusted(self, certificate: x509.Certificate) -> bool:
        if not self._trust_store:
            return False
        verifier = self.policy_builder.store(self._trust_store).build_client_verifier()
        try:
            verifier.verify(certificate, self._issuers)
        except x509.verification.VerificationError:
            return False
        return True


@dataclass
class Pki:
    """Client certificate checks against a trust folder, with the full layout when `root` is a
    `pki_dir` (rejected/, issuers/, crl/, users/), or only `trusted` (`trusted_certs_dir`)."""

    trusted: Path
    issuers: Path | None = None
    crl: Path | None = None
    rejected: Path | None = None
    users: Path | None = None

    @classmethod
    def from_dir(cls, root: Path) -> Pki:
        for sub in PKI_FOLDERS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        return cls(trusted=root / "trusted", issuers=root / "issuers", crl=root / "crl", rejected=root / "rejected",
                   users=root / "users")

    async def validate(self, cert: x509.Certificate, app_description: ua.ApplicationDescription) -> None:
        """asyncua `certificate_validator` hook: raises ServiceError when the client is refused."""
        try:
            if load_certificates(self.trusted):
                issuers = load_certificates(self.issuers) if self.issuers else []
                store = _ChainTrustStore([self.trusted], [self.crl] if self.crl else [], issuers)
                await store.load()
                options = CertificateValidatorOptions.TRUSTED_VALIDATION
            else:      # nothing trusted yet (cryptography refuses an empty store): checks, then refuse
                store, options = None, CertificateValidatorOptions.EXT_VALIDATION
            await CertificateValidator(options | CertificateValidatorOptions.PEER_CLIENT, store).validate(
                cert, app_description)
            if store is None:
                raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)
        except ServiceError as exc:
            saved = self._reject(cert)
            log.warning("opcua_server.client_certificate_refused", subject=common_name(cert),
                        thumbprint=thumbprint(cert.public_bytes(serialization.Encoding.DER)),
                        status=ua.StatusCode(exc.code).name, application_uri=app_description.ApplicationUri,
                        saved_to=str(saved) if saved else None)
            raise

    def _reject(self, cert: x509.Certificate) -> Path | None:
        if self.rejected is None:
            return None
        der = cert.public_bytes(serialization.Encoding.DER)
        name = re.sub(r"[^A-Za-z0-9 ._-]", "_", common_name(cert))[:64].strip() or "client"
        path = self.rejected / f"{name} [{thumbprint(der)}].der"
        with contextlib.suppress(OSError):
            self.rejected.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(der)
            return path
        return None

    def user_name(self, der: bytes) -> str | None:
        """Name of the X.509 user whose certificate (DER) is in users/, None when not trusted."""
        if self.users is None or not der:
            return None
        want = thumbprint(der)
        for cert in load_certificates(self.users):
            if thumbprint(cert.public_bytes(serialization.Encoding.DER)) == want:
                return f"x509:{common_name(cert) or want}"
        return None


def server_certificate_paths(cfg: Any) -> tuple[Path, Path]:
    """(certificate, private key): explicit paths win, else `<pki_dir>/own/...`."""
    if cfg.certificate and cfg.private_key:
        return Path(cfg.certificate), Path(cfg.private_key)
    root = Path(cfg.pki_dir)
    return root / "own" / "cert.der", root / "own" / "private" / "key.pem"


def client_pki(cfg: Any) -> Pki | None:
    """The client certificate checks this config asks for, None when every certificate is
    accepted (no pki_dir, no trusted_certs_dir: access rests on the user login, D-046)."""
    if cfg.pki_dir:
        return Pki.from_dir(Path(cfg.pki_dir))
    if cfg.trusted_certs_dir:
        return Pki(trusted=Path(cfg.trusted_certs_dir))
    return None
