from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

output_dir = Path("./.keys")
output_dir.mkdir(parents=True, exist_ok=True)

key_path = output_dir / "private_key.pem"
cert_path = output_dir / "cert.pem"

# Generate private key
private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)

# Save private key
key_path.write_bytes(
    private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
)

# Certificate subject
subject = issuer = x509.Name(
    [
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ]
)

now = datetime.now(UTC)

cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(private_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now)
    .not_valid_after(now + timedelta(days=30))
    .add_extension(
        x509.SubjectAlternativeName(
            [
                x509.DNSName("localhost"),
                x509.IPAddress(ip_address("127.0.0.1")),
            ]
        ),
        critical=False,
    )
    .sign(private_key, hashes.SHA256())
)

cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

print("Generated:")
print(f"  {key_path}")
print(f"  {cert_path}")
