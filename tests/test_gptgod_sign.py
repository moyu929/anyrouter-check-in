"""GPTGod 签名算法纯函数测试（全离线，不发任何网络请求）。"""

import base64
import json

from utils.gptgod import (
	_credits_info,
	_extract_credits,
	_login_failed_info,
	build_fake_fingerprint,
	extract_sign_params,
	generate_jztz,
	wa_decrypt,
)


def _wa_encrypt(plaintext: str, key: str) -> str:
	"""wa_decrypt 的逆运算，仅用于构造测试数据。"""
	t = [(ord(key[h % len(key)]) ^ 66) + h * 55 & 255 for h in range(16)]
	raw = bytes(ord(c) ^ t[i % 16] for i, c in enumerate(plaintext))
	return base64.b64encode(raw).decode('ascii')


class TestWaDecrypt:
	def test_round_trip(self):
		plaintext = 'var py=[1,2,3];var mc=[4,5,6];function sign(){return 1}'
		key = 'k3yMaterial'

		assert wa_decrypt(_wa_encrypt(plaintext, key), key) == plaintext

	def test_empty_payload(self):
		assert wa_decrypt('', 'anykey') == ''

	def test_longer_than_keystream_period(self):
		# 密钥流周期为 16，明文超过 16 字节可验证 i % 16 的循环取模
		plaintext = 'A' * 40
		key = 'x'

		assert wa_decrypt(_wa_encrypt(plaintext, key), key) == plaintext

	def test_wrong_key_yields_different_plaintext(self):
		cipher = _wa_encrypt('sensitive-js-code', 'right-key')

		assert wa_decrypt(cipher, 'other-key') != 'sensitive-js-code'


class TestExtractSignParams:
	def test_extracts_reorder_table_and_xor_key(self):
		py = list(range(33))
		mc = [11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132, 143, 154, 165, 176]
		js = f'var ywtz = [{",".join(map(str, py))}];\nvar juv = [{",".join(map(str, mc))}];\n'

		assert extract_sign_params(js) == (py, mc)

	def test_returns_none_when_arrays_missing(self):
		assert extract_sign_params('function sign(a){return a}') is None

	def test_returns_none_when_only_reorder_table_present(self):
		js = f'var t = [{",".join(str(i) for i in range(33))}];'

		assert extract_sign_params(js) is None

	def test_float_literals_are_truncated_to_int(self):
		py = [f'{i}.0' for i in range(33)]
		mc = ['1.0'] * 16
		js = f'var a = [{",".join(py)}];var b = [{",".join(mc)}];'

		result = extract_sign_params(js)

		assert result is not None
		assert result[0] == list(range(33))
		assert result[1] == [1] * 16

	def test_ignores_arrays_of_other_lengths(self):
		py = list(range(33))
		mc = [1] * 16
		js = (
			'var noise = [1,2,3];\n'
			f'var a = [{",".join(map(str, py))}];\n'
			f'var b = [{",".join(map(str, mc))}];\n'
			'var more = [9,9,9,9,9];\n'
		)

		assert extract_sign_params(js) == (py, mc)


class TestGenerateJztz:
	def test_reorder_xor_base64_round_trip(self):
		fingerprint = [f'v{i}' for i in range(33)]
		py = list(reversed(range(33)))
		mc = [i * 7 % 256 for i in range(16)]

		token = generate_jztz(fingerprint, py, mc)

		raw = base64.b64decode(token)
		restored = bytes(b ^ mc[i & 15] for i, b in enumerate(raw)).decode('utf-8')
		assert json.loads(restored) == [fingerprint[i] for i in py]

	def test_identity_key_leaves_json_readable(self):
		fingerprint = list(range(33))
		py = list(range(33))
		mc = [0] * 16

		token = generate_jztz(fingerprint, py, mc)

		assert base64.b64decode(token).decode('utf-8') == json.dumps(list(range(33)), separators=(',', ':'))

	def test_output_is_deterministic_for_same_inputs(self):
		fingerprint = [f'v{i}' for i in range(33)]
		py = list(range(33))
		mc = [3] * 16

		assert generate_jztz(fingerprint, py, mc) == generate_jztz(fingerprint, py, mc)

	def test_uses_only_the_first_33_reorder_entries(self):
		fingerprint = [f'v{i}' for i in range(40)]
		py = list(range(40))
		mc = [0] * 16

		token = generate_jztz(fingerprint, py, mc)

		assert json.loads(base64.b64decode(token).decode('utf-8')) == fingerprint[:33]


class TestBuildFakeFingerprint:
	def test_has_33_items(self):
		assert len(build_fake_fingerprint()) == 33

	def test_device_fingerprint_is_stable(self):
		# 设备身份字段（0/1/2/9/10）固定稳定，跨调用不变
		first = build_fake_fingerprint()
		second = build_fake_fingerprint()
		for idx in (0, 1, 2, 9, 10):
			assert first[idx] == second[idx]

	def test_device_identity_is_shared_across_accounts(self):
		# 撤销"按账号持久化 + TTL 轮换"后，所有调用共用同一份稳定设备指纹
		a = build_fake_fingerprint()
		b = build_fake_fingerprint()
		for idx in (0, 1, 2, 9, 10, 26, 27, 28, 31):
			assert a[idx] == b[idx]


class TestCreditsHelpers:
	def test_extract_credits_prefers_tokens(self):
		assert _extract_credits({'tokens': 120, 'credits': 7}) == 120

	def test_extract_credits_falls_back_to_alternate_keys(self):
		assert _extract_credits({'balance': 42}) == 42

	def test_extract_credits_ignores_bool_and_non_numeric(self):
		assert _extract_credits({'tokens': True, 'points': '30', 'balance': 5}) == 5

	def test_extract_credits_returns_none_for_empty_input(self):
		assert _extract_credits(None) is None
		assert _extract_credits({}) is None

	def test_credits_info_marks_unit_as_credits(self):
		info = _credits_info(88)

		assert info['success'] is True
		assert info['quota'] == 88
		assert info['unit'] == 'credits'
		assert '88' in info['display']

	def test_credits_info_treats_none_as_zero(self):
		assert _credits_info(None)['quota'] == 0

	def test_login_failed_info_is_unsuccessful(self):
		info = _login_failed_info()

		assert info['success'] is False
		assert info['unit'] == 'credits'
