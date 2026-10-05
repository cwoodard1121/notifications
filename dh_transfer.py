"""Send a file between PCs over the Agent Bus with Diffie-Hellman (stdlib only).
receiver: python dh_transfer.py init                    -> prints PUB (send it to the sender)
sender:   python dh_transfer.py seal <PUB> <file> <out.txt>   -> bus-sized DHX parts, one per line
receiver: python dh_transfer.py open <out.zip> [parts.txt]    -> no parts.txt: reads them from the local bus DB
RFC 3526 group 14 (2048-bit MODP), HKDF-SHA256, HMAC-SHA256 CTR + encrypt-then-MAC tag."""
import base64, hashlib, hmac, os, pathlib, re, secrets, sqlite3, sys
P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
        "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
KEY = pathlib.Path.home() / ".dh_transfer.key"
CHUNK = 3500
b64 = lambda b: base64.urlsafe_b64encode(b).decode()


def hk(ikm, salt, info):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def keys(peer, mine, a_pub, b_pub):
    if not 1 < peer < P - 1 or pow(peer, (P - 1) // 2, P) != 1:
        sys.exit("bad public value")
    s = pow(peer, mine, P).to_bytes(256, "big")
    salt = a_pub.to_bytes(256, "big") + b_pub.to_bytes(256, "big")
    return hk(s, salt, b"enc"), hk(s, salt, b"mac")


def stream(k, nonce, n):
    out = b"".join(hmac.new(k, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest() for i in range(n // 32 + 1))
    return out[:n]


def main(cmd, *a):
    if cmd == "init":
        x = secrets.randbelow(P - 4) + 2
        KEY.write_text(str(x))
        print(f"{pow(2, x, P):x}")
    elif cmd == "seal":
        b_pub, data = int(a[0].strip(), 16), pathlib.Path(a[1]).read_bytes()
        x = secrets.randbelow(P - 4) + 2
        a_pub = pow(2, x, P)
        ke, km = keys(b_pub, x, a_pub, b_pub)
        nonce = os.urandom(16)
        ct = bytes(p ^ q for p, q in zip(data, stream(ke, nonce, len(data))))
        tag = hmac.new(km, nonce + ct, hashlib.sha256).digest()
        tok = f"{a_pub:x}.{b_pub:x}.{b64(nonce + ct + tag)}"
        sid = hashlib.sha256(tok.encode()).hexdigest()[:12]
        parts = [tok[i:i + CHUNK] for i in range(0, len(tok), CHUNK)]
        pathlib.Path(a[2]).write_text("\n".join(f"DHX|{sid}|{i + 1}|{len(parts)}|{p}" for i, p in enumerate(parts)) + "\n")
        print(f"{len(parts)} parts, id {sid}, plaintext sha256 {hashlib.sha256(data).hexdigest()}")
    elif cmd == "open":
        if len(a) > 1:
            text = pathlib.Path(a[1]).read_text()
        else:
            db = pathlib.Path(__file__).with_name("data") / "notifications.sqlite3"
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            text = "\n".join(r[0] for r in con.execute("select body from messages where body like 'DHX|%'"))
        found = {}
        for sid, i, n, p in re.findall(r"DHX\|(\w{12})\|(\d+)\|(\d+)\|([\w.=-]+)", text):
            found.setdefault(sid, [int(n), {}])[1][int(i)] = p
        x = int(KEY.read_text())
        mine = pow(2, x, P)
        for sid, (n, got) in found.items():
            tok = "".join(got.get(i + 1, "") for i in range(n))
            if len(got) < n or tok.split(".")[1] != f"{mine:x}":
                print(f"{sid}: {len(got)}/{n} parts" + ("" if len(got) < n else " (for another key)"))
                continue
            a_pub, _, blob = tok.split(".")
            blob = base64.urlsafe_b64decode(blob)
            nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
            ke, km = keys(int(a_pub, 16), x, int(a_pub, 16), mine)
            if not hmac.compare_digest(tag, hmac.new(km, nonce + ct, hashlib.sha256).digest()):
                sys.exit(f"{sid}: authentication FAILED")
            data = bytes(p ^ q for p, q in zip(ct, stream(ke, nonce, len(ct))))
            pathlib.Path(a[0]).write_bytes(data)
            sys.exit(print(f"OK {sid}: wrote {a[0]}, sha256 {hashlib.sha256(data).hexdigest()}"))
        sys.exit("no complete transfer for this key yet")


if __name__ == "__main__":
    main(*sys.argv[1:]) if len(sys.argv) > 1 else print(__doc__)
