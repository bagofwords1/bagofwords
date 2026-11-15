"""Parquet modular encryption backed by BOW's artifact key wrapping."""

import base64

from pyarrow import parquet as pq
from pyarrow.parquet import encryption as pe

from app.data_sources.fast.artifacts import encrypt_key, decrypt_key


class _ArtifactKms(pe.KmsClient):
    def wrap_key(self, key_bytes, master_key_identifier):
        return encrypt_key(base64.b64encode(key_bytes).decode("ascii")).encode("ascii")

    def unwrap_key(self, wrapped_key, master_key_identifier):
        return base64.b64decode(decrypt_key(wrapped_key))


_factory = pe.CryptoFactory(lambda config: _ArtifactKms())
_kms = pe.KmsConnectionConfig()


def encryption_properties():
    return _factory.file_encryption_properties(
        _kms, pe.EncryptionConfiguration(footer_key="bow", uniform_encryption=True, plaintext_footer=False)
    )


def decryption_properties():
    return _factory.file_decryption_properties(_kms)


def encrypt_parquet(source, dest):
    with (
        pq.ParquetFile(source) as reader,
        pq.ParquetWriter(
            dest, reader.schema_arrow, compression="zstd", encryption_properties=encryption_properties()
        ) as writer,
    ):
        for batch in reader.iter_batches():
            writer.write_batch(batch)
