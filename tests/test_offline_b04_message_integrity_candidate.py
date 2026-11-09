import unittest
from dataclasses import replace
from sora_commander_next.offline_b04_message_integrity_candidate import (
    MessageEnvelope, MessageEvidenceDenied, OfflineSequenceReview, demo_mac,
)
K = bytes(range(32))
A = "a"*64
B = "b"*64

def env(**kw):
    values = dict(epoch=7,direction="client-to-service",sequence=1,
                  client_nonce=A,server_nonce=B,payload=b"synthetic")
    values.update(kw)
    return MessageEnvelope(**values)

def reviewer():
    return OfflineSequenceReview(epoch=7,client_nonce=A,server_nonce=B)

class MessageIntegrityTests(unittest.TestCase):
    def test_valid_sequence_is_not_authorization(self):
        r=reviewer()
        for seq in range(1,5):
            e=env(sequence=seq)
            v=r.review(K,e,demo_mac(K,e))
            self.assertTrue(v.mac_matches)
            self.assertTrue(v.next_sequence)
            self.assertEqual(v.authentication,"NOT_AUTHENTICATED")
            self.assertEqual(v.admission,"UNAVAILABLE")
    def test_duplicate_and_out_of_order_denied(self):
        r=reviewer();e=env()
        self.assertTrue(r.review(K,e,demo_mac(K,e)).next_sequence)
        self.assertFalse(r.review(K,e,demo_mac(K,e)).next_sequence)
        late=env(sequence=3)
        self.assertFalse(r.review(K,late,demo_mac(K,late)).next_sequence)
        second=env(sequence=2)
        self.assertTrue(r.review(K,second,demo_mac(K,second)).next_sequence)
    def test_direction_sequences_independent(self):
        r=reviewer()
        for direction in ("client-to-service","service-to-client"):
            e=env(direction=direction)
            self.assertTrue(r.review(K,e,demo_mac(K,e)).next_sequence)
    def test_every_context_change_denied(self):
        r=reviewer()
        e=env();valid=demo_mac(K,e)
        for field,value in (("epoch",8),("direction","service-to-client"),
              ("sequence",2),("client_nonce","c"*64),
              ("server_nonce","d"*64),("payload",b"tampered")):
            with self.subTest(field=field):
                v=r.review(K,replace(e,**{field:value}),valid)
                self.assertFalse(v.mac_matches)
                self.assertFalse(v.next_sequence)
    def test_wrong_key_and_corrupted_mac(self):
        r=reviewer();e=env();tag=demo_mac(K,e)
        self.assertFalse(r.review(K,e,demo_mac(b"x"*32,e)).mac_matches)
        self.assertFalse(r.review(K,e,bytes([tag[0]^1])+tag[1:]).mac_matches)
        self.assertTrue(r.review(K,e,tag).next_sequence)
    def test_invalid_input_strict(self):
        for field,value in (("epoch",True),("sequence",0),("direction","other"),
             ("client_nonce","xyz"),("server_nonce",A),("payload",bytearray(b"x")),
             ("payload",b"x"*4097)):
            with self.subTest(field=field):
                with self.assertRaises(MessageEvidenceDenied):env(**{field:value})
        with self.assertRaises(MessageEvidenceDenied):demo_mac(b"x",env())
        with self.assertRaises(MessageEvidenceDenied):reviewer().review(K,env(),b"bad")
    def test_cross_epoch_replay_even_with_valid_mac_denied(self):
        r=reviewer();old=env(epoch=8)
        v=r.review(K,old,demo_mac(K,old))
        self.assertFalse(v.mac_matches)
        self.assertFalse(v.next_sequence)

    def test_failed_verification_does_not_advance_sequence(self):
        r=reviewer()
        one=env()
        self.assertFalse(r.review(K,one,b"0"*32).next_sequence)
        self.assertTrue(r.review(K,one,demo_mac(K,one)).next_sequence)
        two=env(sequence=2)
        self.assertFalse(r.review(K,two,demo_mac(b"z"*32,two)).next_sequence)
        self.assertTrue(r.review(K,two,demo_mac(K,two)).next_sequence)

    def test_sequence_wrap_is_rejected(self):
        r=reviewer()
        huge=env(sequence=(1<<63)-1)
        self.assertFalse(r.review(K,huge,demo_mac(K,huge)).next_sequence)
        with self.assertRaises(MessageEvidenceDenied):
            env(sequence=1<<63)

    def test_cross_direction_tag_is_not_interchangeable(self):
        original=env()
        tag=demo_mac(K,original)
        other=env(direction="service-to-client")
        r=reviewer()
        self.assertFalse(r.review(K,other,tag).mac_matches)
        self.assertTrue(r.review(K,other,demo_mac(K,other)).next_sequence)
