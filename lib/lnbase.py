#!/usr/bin/env python3
"""
  Lightning network interface for Electrum
  Derived from https://gist.github.com/AdamISZ/046d05c156aaeb56cc897f85eecb3eb8
"""

from ecdsa.util import sigdecode_der, sigencode_string_canonize
from ecdsa import VerifyingKey
from ecdsa.curves import SECP256k1
import subprocess
import queue
import traceback
import itertools
import json
from collections import OrderedDict
import asyncio
import sys
import os
import time
import binascii
import hashlib
import hmac
import cryptography.hazmat.primitives.ciphers.aead as AEAD

from .bitcoin import (public_key_from_private_key, ser_to_point, point_to_ser,
                      string_to_number, deserialize_privkey, EC_KEY, rev_hex, int_to_hex,
                      push_script, script_num_to_hex, add_data_to_script,
                      add_number_to_script)
from . import bitcoin
from . import constants
from . import transaction
from .util import PrintError, bh2u, print_error, bfh
from .transaction import opcodes, Transaction

# hardcoded nodes
node_list = [
    ('ecdsa.net', '9735', '038370f0e7a03eded3e1d41dc081084a87f0afa1c5b22090b4f3abb391eb15d8ff'),
    ('77.58.162.148', '9735', '022bb78ab9df617aeaaf37f6644609abb7295fad0c20327bccd41f8d69173ccb49')
]


class LightningError(Exception):
    pass

message_types = {}

def handlesingle(x, ma):
    try:
        x = int(x)
    except ValueError:
        x = ma[x]
    try:
        x = int(x)
    except ValueError:
        x = int.from_bytes(x, byteorder="big")
    return x

def calcexp(exp, ma):
    exp = str(exp)
    assert "*" not in exp
    return sum(handlesingle(x, ma) for x in exp.split("+"))

def make_handler(k, v):
    def handler(data):
        nonlocal k, v
        ma = {}
        pos = 0
        for fieldname in v["payload"]:
            poslenMap = v["payload"][fieldname]
            if "feature" in poslenMap: continue
            #print(poslenMap["position"], ma)
            assert pos == calcexp(poslenMap["position"], ma)
            length = poslenMap["length"]
            length = calcexp(length, ma)
            ma[fieldname] = data[pos:pos+length]
            pos += length
        assert pos == len(data), (k, pos, len(data))
        return k, ma
    return handler

path = os.path.join(os.path.dirname(__file__), 'lightning.json')
with open(path) as f:
    structured = json.loads(f.read(), object_pairs_hook=OrderedDict)

for k in structured:
    v = structured[k]
    if k in ["final_incorrect_cltv_expiry", "final_incorrect_htlc_amount"]:
        continue
    if len(v["payload"]) == 0:
        continue
    try:
        num = int(v["type"])
    except ValueError:
        #print("skipping", k)
        continue
    byts = num.to_bytes(byteorder="big",length=2)
    assert byts not in message_types, (byts, message_types[byts].__name__, k)
    names = [x.__name__ for x in message_types.values()]
    assert k + "_handler" not in names, (k, names)
    message_types[byts] = make_handler(k, v)
    message_types[byts].__name__ = k + "_handler"

assert message_types[b"\x00\x10"].__name__ == "init_handler"

def decode_msg(data):
    typ = data[:2]
    k, parsed = message_types[typ](data[2:])
    return k, parsed

def gen_msg(msg_type, **kwargs):
    typ = structured[msg_type]
    data = int(typ["type"]).to_bytes(byteorder="big", length=2)
    lengths = {}
    for k in typ["payload"]:
        poslenMap = typ["payload"][k]
        if "feature" in poslenMap: continue
        leng = calcexp(poslenMap["length"], lengths)
        try:
            clone = dict(lengths)
            clone.update(kwargs)
            leng = calcexp(poslenMap["length"], clone)
        except KeyError:
            pass
        try:
            param = kwargs[k]
        except KeyError:
            param = 0
        try:
            if not isinstance(param, bytes):
                assert isinstance(param, int), "field {} is neither bytes or int".format(k)
                param = param.to_bytes(length=leng, byteorder="big")
        except ValueError:
            raise Exception("{} does not fit in {} bytes".format(k, leng))
        lengths[k] = len(param)
        if lengths[k] != leng:
            raise Exception("field {} is {} bytes long, should be {} bytes long".format(k, lengths[k], leng))
        data += param
    return data

def encode(n, s):
    """Return a bytestring version of the integer
    value n, with a string length of s
    """
    return n.to_bytes(length=s, byteorder="big")


def H256(data):
    return hashlib.sha256(data).digest()

class HandshakeState(object):
    prologue = b"lightning"
    protocol_name = b"Noise_XK_secp256k1_ChaChaPoly_SHA256"
    handshake_version = b"\x00"
    def __init__(self, responder_pub):
        self.responder_pub = responder_pub
        self.h = H256(self.protocol_name)
        self.ck = self.h
        self.update(self.prologue)
        self.update(self.responder_pub)

    def update(self, data):
        self.h = H256(self.h + data)
        return self.h

def get_nonce_bytes(n):
    """BOLT 8 requires the nonce to be 12 bytes, 4 bytes leading
    zeroes and 8 bytes little endian encoded 64 bit integer.
    """
    nb = b"\x00"*4
    #Encode the integer as an 8 byte byte-string
    nb2 = encode(n, 8)
    nb2 = bytearray(nb2)
    #Little-endian is required here
    nb2.reverse()
    return nb + nb2

def aead_encrypt(k, nonce, associated_data, data):
    nonce_bytes = get_nonce_bytes(nonce)
    a = AEAD.ChaCha20Poly1305(k)
    return a.encrypt(nonce_bytes, data, associated_data)

def aead_decrypt(k, nonce, associated_data, data):
    nonce_bytes = get_nonce_bytes(nonce)
    a = AEAD.ChaCha20Poly1305(k)
    #raises InvalidTag exception if it's not valid
    return a.decrypt(nonce_bytes, data, associated_data)

def get_bolt8_hkdf(salt, ikm):
    """RFC5869 HKDF instantiated in the specific form
    used in Lightning BOLT 8:
    Extract and expand to 64 bytes using HMAC-SHA256,
    with info field set to a zero length string as per BOLT8
    Return as two 32 byte fields.
    """
    #Extract
    prk = hmac.new(salt, msg=ikm, digestmod=hashlib.sha256).digest()
    assert len(prk) == 32
    #Expand
    info = b""
    T0 = b""
    T1 = hmac.new(prk, T0 + info + b"\x01", digestmod=hashlib.sha256).digest()
    T2 = hmac.new(prk, T1 + info + b"\x02", digestmod=hashlib.sha256).digest()
    assert len(T1 + T2) == 64
    return T1, T2

def get_ecdh(priv, pub):
    s = string_to_number(priv)
    pk = ser_to_point(pub)
    pt = point_to_ser(pk * s)
    return H256(pt)

def act1_initiator_message(hs, my_privkey):
    #Get a new ephemeral key
    epriv, epub = create_ephemeral_key(my_privkey)
    hs.update(epub)
    ss = get_ecdh(epriv, hs.responder_pub)
    ck2, temp_k1 = get_bolt8_hkdf(hs.ck, ss)
    hs.ck = ck2
    c = aead_encrypt(temp_k1, 0, hs.h, b"")
    #for next step if we do it
    hs.update(c)
    msg = hs.handshake_version + epub + c
    assert len(msg) == 50
    return msg

def privkey_to_pubkey(priv):
    pub = public_key_from_private_key(priv[:32], True)
    return bytes.fromhex(pub)

def create_ephemeral_key(privkey):
    pub = privkey_to_pubkey(privkey)
    return (privkey[:32], pub)

def get_unused_keys():
    xprv, xpub = bitcoin.bip32_root(b"testseed", "p2wpkh")
    for i in itertools.count():
        childxprv, childxpub = bitcoin.bip32_private_derivation(xprv, "m/", "m/42/"+str(i))
        _, _, _, _, child_c, child_cK = bitcoin.deserialize_xpub(childxpub)
        _, _, _, _, _, k = bitcoin.deserialize_xprv(childxprv)
        assert len(k) == 32
        yield child_cK, k

def aiosafe(f):
    async def f2(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except:
            # if the loop isn't stopped
            # run_forever in network.py would not return,
            # the asyncioThread would not die,
            # and we would block on shutdown
            asyncio.get_event_loop().stop()
            traceback.print_exc()
    return f2

def get_obscured_ctn(ctn, local, remote):
    mask = int.from_bytes(H256(local + remote)[-6:], byteorder="big")
    return ctn ^ mask

def overall_weight(num_htlc):
    return 500 + 172 * num_htlc + 224

def make_commitment(ctn, local_funding_pubkey, remote_funding_pubkey, remotepubkey,
                    payment_pubkey, remote_payment_pubkey, revocation_pubkey, delayed_pubkey,
                    funding_txid, funding_pos, funding_satoshis,
                    to_local_msat, to_remote_msat, local_feerate, local_delay):
    pubkeys = sorted([bh2u(local_funding_pubkey), bh2u(remote_funding_pubkey)])
    obs = get_obscured_ctn(ctn, payment_pubkey, remote_payment_pubkey)
    locktime = (0x20 << 24) + (obs & 0xffffff)
    sequence = (0x80 << 24) + (obs >> 24)
    print_error('locktime', locktime, hex(locktime))
    # commitment tx input
    c_inputs = [{
        'type': 'p2wsh',
        'x_pubkeys': pubkeys,
        'signatures':[None, None],
        'num_sig': 2,
        'prevout_n': funding_pos,
        'prevout_hash': funding_txid,
        'value': funding_satoshis,
        'coinbase': False,
        'sequence':sequence
    }]
    # commitment tx outputs
    local_script = bytes([opcodes.OP_IF]) + bfh(push_script(bh2u(revocation_pubkey))) + bytes([opcodes.OP_ELSE]) + add_number_to_script(local_delay) \
                   + bytes([opcodes.OP_CSV, opcodes.OP_DROP]) + bfh(push_script(bh2u(delayed_pubkey))) + bytes([opcodes.OP_ENDIF, opcodes.OP_CHECKSIG])
    local_address = bitcoin.redeem_script_to_address('p2wsh', bh2u(local_script))
    fee = local_feerate * overall_weight(0) // 1000
    local_amount = to_local_msat // 1000 - fee
    remote_address = bitcoin.pubkey_to_address('p2wpkh', bh2u(remotepubkey))
    remote_amount = to_remote_msat // 1000
    to_local = (bitcoin.TYPE_ADDRESS, local_address, local_amount)
    to_remote = (bitcoin.TYPE_ADDRESS, remote_address, remote_amount)
    # no htlc for the moment
    c_outputs = [to_local, to_remote]
    # create commitment tx
    tx = Transaction.from_io(c_inputs, c_outputs, locktime=locktime, version=2)
    tx.BIP_LI01_sort()
    return tx

class Peer(PrintError):

    def __init__(self, host, port, pubkey, request_initial_sync=True):
        self.host = host
        self.port = port
        self.privkey = os.urandom(32) + b"\x01"
        self.pubkey = pubkey
        self.read_buffer = b''
        self.ping_time = 0
        self.channel_accepted = {}
        self.funding_signed = {}
        self.initialized = asyncio.Future()
        self.localfeatures = (0x08 if request_initial_sync else 0)
        # view of the network
        self.nodes = {} # received node announcements
        self.channels = {} # received channel announcements
        self.channel_u_origin = {}
        self.channel_u_final = {}

    def diagnostic_name(self):
        return self.host

    def ping_if_required(self):
        if time.time() - self.ping_time > 120:
            self.send_message(gen_msg('ping', num_pong_bytes=4, byteslen=4))
            self.ping_time = time.time()

    def send_message(self, msg):
        message_type, payload = decode_msg(msg)
        self.print_error("Sending '%s'"%message_type.upper(), payload)
        l = encode(len(msg), 2)
        lc = aead_encrypt(self.sk, self.sn(), b'', l)
        c = aead_encrypt(self.sk, self.sn(), b'', msg)
        assert len(lc) == 18
        assert len(c) == len(msg) + 16
        self.writer.write(lc+c)

    async def read_message(self):
        rn_l, rk_l = self.rn()
        rn_m, rk_m = self.rn()
        while True:
            s = await self.reader.read(2**10)
            if not s:
                raise Exception('connection closed')
            self.read_buffer += s
            if len(self.read_buffer) < 18:
                continue
            lc = self.read_buffer[:18]
            l = aead_decrypt(rk_l, rn_l, b'', lc)
            length = int.from_bytes(l, byteorder="big")
            offset = 18 + length + 16
            if len(self.read_buffer) < offset:
                continue
            c = self.read_buffer[18:offset]
            self.read_buffer = self.read_buffer[offset:]
            msg = aead_decrypt(rk_m, rn_m, b'', c)
            return msg

    async def handshake(self):
        hs = HandshakeState(self.pubkey)
        msg = act1_initiator_message(hs, self.privkey)
        # act 1
        self.writer.write(msg)
        rspns = await self.reader.read(2**10)
        assert len(rspns) == 50
        hver, alice_epub, tag = rspns[0], rspns[1:34], rspns[34:]
        assert bytes([hver]) == hs.handshake_version
        # act 2
        hs.update(alice_epub)
        myepriv, myepub = create_ephemeral_key(self.privkey)
        ss = get_ecdh(myepriv, alice_epub)
        ck, temp_k2 = get_bolt8_hkdf(hs.ck, ss)
        hs.ck = ck
        p = aead_decrypt(temp_k2, 0, hs.h, tag)
        hs.update(tag)
        # act 3
        my_pubkey = privkey_to_pubkey(self.privkey)
        c = aead_encrypt(temp_k2, 1, hs.h, my_pubkey)
        hs.update(c)
        ss = get_ecdh(self.privkey[:32], alice_epub)
        ck, temp_k3 = get_bolt8_hkdf(hs.ck, ss)
        hs.ck = ck
        t = aead_encrypt(temp_k3, 0, hs.h, b'')
        self.sk, self.rk = get_bolt8_hkdf(hs.ck, b'')
        msg = hs.handshake_version + c + t
        self.writer.write(msg)
        # init counters
        self._sn = 0
        self._rn = 0
        self.r_ck = ck
        self.s_ck = ck

    def rn(self):
        o = self._rn, self.rk
        self._rn += 1
        if self._rn == 1000:
            self.r_ck, self.rk = get_bolt8_hkdf(self.r_ck, self.rk)
            self._rn = 0
        return o

    def sn(self):
        o = self._sn
        self._sn += 1
        if self._sn == 1000:
            self.s_ck, self.sk = get_bolt8_hkdf(self.s_ck, self.sk)
            self._sn = 0
        return o

    def process_message(self, message):
        message_type, payload = decode_msg(message)
        try:
            f = getattr(self, 'on_' + message_type)
        except AttributeError:
            self.print_error("Received '%s'" % message_type.upper(), payload)
            return
        # raw message is needed to check signature
        if message_type=='node_announcement':
            payload['raw'] = message
        f(payload)

    def on_error(self, payload):
        if payload["channel_id"] in self.channel_accepted:
            self.channel_accepted[payload["channel_id"]].set_exception(LightningError(payload["data"]))
        if payload["channel_id"] in self.funding_signed:
            self.funding_signed[payload["channel_id"]].set_exception(LightningError(payload["data"]))

    def on_ping(self, payload):
        l = int.from_bytes(payload['num_pong_bytes'], byteorder="big")
        self.send_message(gen_msg('pong', byteslen=l))

    def on_accept_channel(self, payload):
        self.channel_accepted[payload["temporary_channel_id"]].set_result(payload)

    def on_funding_signed(self, payload):
        sig = payload['signature']
        channel_id = payload['channel_id']
        tx = self.channels[channel_id]
        self.network.broadcast(tx)

    def on_funding_signed(self, payload):
        self.funding_signed[payload["temporary_channel_id"]].set_result(payload)

    def on_funding_locked(self, payload):
        pass

    def on_node_announcement(self, payload):
        pubkey = payload['node_id']
        signature = payload['signature']
        h = bitcoin.Hash(payload['raw'][66:])
        if not bitcoin.verify_signature(pubkey, signature, h):
            return False
        self.s = payload['addresses']
        def read(n):
            data, self.s = self.s[0:n], self.s[n:]
            return data
        addresses = []
        while self.s:
            atype = ord(read(1))
            if atype == 0:
                pass
            elif atype == 1:
                ipv4_addr = '.'.join(map(lambda x: '%d'%x, read(4)))
                port = int.from_bytes(read(2), byteorder="big")
                x = ipv4_addr, port, binascii.hexlify(pubkey)
                addresses.append((ipv4_addr, port))
            elif atype == 2:
                ipv6_addr = b':'.join([binascii.hexlify(read(2)) for i in range(4)])
                port = int.from_bytes(read(2), byteorder="big")
                addresses.append((ipv6_addr, port))
            else:
                pass
            continue
        alias = payload['alias'].rstrip(b'\x00')
        self.nodes[pubkey] = {
            'alias': alias,
            'addresses': addresses
        }
        self.print_error('node announcement', binascii.hexlify(pubkey), alias, addresses)

    def on_init(self, payload):
        pass

    def on_channel_update(self, payload):
        flags = int.from_bytes(payload['flags'], byteorder="big")
        direction = bool(flags & 1)
        short_channel_id = payload['short_channel_id']
        if direction:
            self.channel_u_origin[short_channel_id] = payload
        else:
            self.channel_u_final[short_channel_id] = payload
        self.print_error('channel update', binascii.hexlify(short_channel_id), flags)

    def on_channel_announcement(self, payload):
        short_channel_id = payload['short_channel_id']
        self.print_error('channel announcement', binascii.hexlify(short_channel_id))
        self.channels[short_channel_id] = payload

    #def open_channel(self, funding_sat, push_msat):
    #    self.send_message(gen_msg('open_channel', funding_satoshis=funding_sat, push_msat=push_msat))

    @aiosafe
    async def main_loop(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        await self.handshake()
        # send init
        self.send_message(gen_msg("init", gflen=0, lflen=1, localfeatures=self.localfeatures))
        # read init
        msg = await self.read_message()
        self.process_message(msg)
        # initialized
        self.initialized.set_result(msg)
        # loop
        while True:
            self.ping_if_required()
            msg = await self.read_message()
            self.process_message(msg)
        # close socket
        self.print_error('closing lnbase')
        self.writer.close()

    @aiosafe
    async def channel_establishment_flow(self, wallet, config):
        await self.initialized
        keys = get_unused_keys()
        temp_channel_id = os.urandom(32)
        funding_pubkey, funding_privkey = next(keys)
        revocation_pubkey, revocation_privkey = next(keys)
        htlc_pubkey, htlc_privkey = next(keys)
        payment_pubkey, payment_privkey = next(keys)
        delayed_pubkey, delayed_privkey = next(keys)
        funding_satoshis = 20000
        msg = gen_msg(
            "open_channel",
            temporary_channel_id=temp_channel_id,
            chain_hash=bytes.fromhex(rev_hex(constants.net.GENESIS)),
            funding_satoshis=funding_satoshis,
            max_accepted_htlcs=5,
            funding_pubkey=funding_pubkey,
            revocation_basepoint=revocation_pubkey,
            htlc_basepoint=htlc_pubkey,
            payment_basepoint=payment_pubkey,
            delayed_payment_basepoint=delayed_pubkey,
            first_per_commitment_point=next(keys)[0]
        )
        self.channel_accepted[temp_channel_id] = asyncio.Future()
        self.send_message(msg)
        try:
            accept_channel = await self.channel_accepted[temp_channel_id]
        finally:
            del self.channel_accepted[temp_channel_id]
        remote_funding_pubkey = accept_channel["funding_pubkey"]
        pubkeys = sorted([bh2u(funding_pubkey), bh2u(remote_pubkey)])
        redeem_script = transaction.multisig_script(pubkeys, 2)
        funding_address = bitcoin.redeem_script_to_address('p2wsh', redeem_script)
        funding_output = (bitcoin.TYPE_ADDRESS, funding_address, funding_satoshis)
        funding_tx = wallet.mktx([funding_output], None, config, 1000)
        funding_index = funding_tx.outputs().index(funding_output)
        remote_payment_pubkey = accept_channel['payment_basepoint']
        c_tx = make_commitment(
            0,
            funding_pubkey, remote_funding_pubkey,
            payment_pubkey, remote_payment_pubkey, revocation_pubkey, delayed_pubkey,
            funding_tx.txid(), funding_index, funding_satoshis,
            funding_satoshis*1000, 0, 20000, 144)
        c_tx.sign({bh2u(funding_pubkey): (funding_privkey, True)})
        sig_index = pubkeys.index(bh2u(funding_pubkey))
        sig = bytes.fromhex(c_tx.inputs()[0]["signatures"][sig_index])
        self.print_error('sig', len(sig))
        sig = bytes(sig[:len(sig)-1])
        r, s = sigdecode_der(sig, SECP256k1.generator.order())
        sig = sigencode_string_canonize(r, s, SECP256k1.generator.order())
        self.print_error('canonical signature', len(sig))
        self.funding_signed[temp_channel_id] = asyncio.Future()
        self.send_message(gen_msg("funding_created", temporary_channel_id=temp_channel_id, funding_txid=bytes.fromhex(funding_tx.txid()), funding_output_index=funding_index, signature=sig))
        try:
            funding_signed = await self.funding_signed[temp_channel_id]
        finally:
            del self.funding_signed[temp_channel_id]


# replacement for lightningCall
class LNWorker:

    def __init__(self, wallet, network):
        self.wallet = wallet
        self.network = network
        host, port, pubkey = network.config.get('lightning_peer', node_list[0])
        pubkey = binascii.unhexlify(pubkey)
        port = int(port)
        self.peer = Peer(host, port, pubkey)
        self.network.futures.append(asyncio.run_coroutine_threadsafe(self.peer.main_loop(), asyncio.get_event_loop()))

    def openchannel(self):
        # todo: get utxo from wallet
        # submit coro to asyncio main loop
        self.peer.open_channel()