# Galileo OSNMA sample data

`ublox_run/` is taken unchanged from OSNMAlib v3.2.5, folder `ublox_run/`:
https://github.com/Algafix/OSNMA/tree/v3.2.5/ublox_run

- `current_config.ubx` - real u-blox recording (UBX-RXM-SFRBX, Galileo I/NAV E1-B), 9 March 2026.
- `OSNMA_PublicKey_2.xml` - public key published by the European GNSS Service Centre (GSC), PKID 2.
- `OSNMA_MerkleTree.xml` - GSC Merkle tree root used to verify that key.

OSNMAlib (https://osnmalib.eu): A. Galan-Figueras (KU Leuven), I. Fernandez-Hernandez,
C. Iniguez, G. Seco-Granados and contributors. Licensed under the European Union Public
Licence v1.2, full text in `LICENSE-EUPL-1.2.txt`.

The forged sample used in the demo is not stored here. The backend derives it at runtime from
`current_config.ubx` (`services/osnma.py`, `forge_ubx`) by flipping one ephemeris bit (M0, word
type 1) of a few satellites and fixing the UBX checksums.
