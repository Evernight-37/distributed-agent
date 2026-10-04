"""Rooted device identities. Private material is encrypted only on the local device."""
import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def encode(value): return base64.b64encode(value).decode('ascii')
def decode(value,length=None):
    if not isinstance(value,str) or len(value)>10000: raise ValueError('Invalid encoded key')
    result=base64.b64decode(value,validate=True)
    if length is not None and len(result)!=length: raise ValueError('Invalid key length')
    return result

def account_id(public_key): return 'account-'+hashlib.sha256(decode(public_key,32)).hexdigest()
def generate_identity():
    key=Ed25519PrivateKey.generate();public=encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw))
    return {'private_key':encode(key.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())),'public_key':public,'chain':[],'account_id':account_id(public)}
def sign_envelope(identity,body):
    if not isinstance(body,dict): raise ValueError('Body must be an object')
    return {'body':body,'public_key':identity['public_key'],'chain':identity.get('chain',[]),'signature':encode(Ed25519PrivateKey.from_private_bytes(decode(identity['private_key'],32)).sign(canonical(body)))}
def certify_device(identity,child_public_key):
    decode(child_public_key,32)
    body={'domain':'harness-device-certificate-v1','account_id':identity['account_id'],'parent':identity['public_key'],'child':child_public_key}
    return {'body':body,'signature':sign_envelope(identity,body)['signature']}

def certify_delegate(identity,child_public_key,scopes):
    """Create a leaf-only, capability-scoped delegation certificate.

    Delegates are intended for unattended local integrations such as the
    Codex MCP adapter.  They are deliberately distinct from device
    certificates: a delegate can authenticate account requests but must never
    inherit machine-owner or account-administration authority.
    """
    decode(child_public_key,32)
    if not isinstance(scopes,(list,tuple,set)) or not scopes:
        raise ValueError('Delegate scopes are required')
    checked=sorted(set(scopes))
    if len(checked)>16 or any(not isinstance(scope,str) or not 1<=len(scope)<=80 for scope in checked):
        raise ValueError('Invalid delegate scopes')
    body={'domain':'harness-delegate-certificate-v1','account_id':identity['account_id'],
          'parent':identity['public_key'],'child':child_public_key,'scopes':checked}
    return {'body':body,'signature':sign_envelope(identity,body)['signature']}
def verify_envelope(envelope,expected_account=None):
    if not isinstance(envelope,dict) or not isinstance(envelope.get('body'),dict): raise PermissionError('Invalid signed envelope')
    public=envelope.get('public_key');decode(public,32);chain=envelope.get('chain',[])
    if not isinstance(chain,list) or len(chain)>8: raise PermissionError('Invalid certificate chain')
    root=chain[0]['body']['parent'] if chain else public
    account=account_id(root);parent=root;seen={root}
    if expected_account is not None and account!=expected_account: raise PermissionError('Wrong account root')
    scopes=None
    try:
        for index,cert in enumerate(chain):
            body=cert['body']
            device=(set(body)=={'domain','account_id','parent','child'} and body.get('domain')=='harness-device-certificate-v1')
            delegate=(set(body)=={'domain','account_id','parent','child','scopes'} and body.get('domain')=='harness-delegate-certificate-v1')
            if not device and not delegate: raise PermissionError('Invalid device certificate')
            if body['account_id']!=account or body['parent']!=parent or body['child'] in seen: raise PermissionError('Invalid device certificate')
            if delegate:
                if index!=len(chain)-1: raise PermissionError('Delegate certificate must be the leaf')
                values=body.get('scopes')
                if not isinstance(values,list) or not values or len(values)>16 or values!=sorted(set(values)) or any(not isinstance(value,str) or not 1<=len(value)<=80 for value in values):
                    raise PermissionError('Invalid delegate scopes')
                scopes=tuple(values)
            Ed25519PublicKey.from_public_bytes(decode(parent,32)).verify(decode(cert['signature'],64),canonical(body));parent=body['child'];decode(parent,32);seen.add(parent)
        if parent!=public: raise PermissionError('Unrooted device identity')
        Ed25519PublicKey.from_public_bytes(decode(public,32)).verify(decode(envelope['signature'],64),canonical(envelope['body']))
    except PermissionError: raise
    except Exception: raise PermissionError('Signature verification failed') from None
    return {'account_id':account,'public_key':public,'root_public_key':root,'body':envelope['body'],
            'chain_keys':list(seen),'delegate_scopes':scopes}
def verify_owner_command(envelope,expected_account):
    verified=verify_envelope(envelope,expected_account);body=verified['body']
    if body.get('domain')!='harness-owner-command-v1' or body.get('account_id')!=expected_account or not isinstance(body.get('id'),str) or not isinstance(body.get('target'),str): raise PermissionError('Invalid owner command domain')
    return body

def encrypt_profile(profile,password):
    if not isinstance(password,str): raise ValueError('Password must be text')
    salt=os.urandom(16);nonce=os.urandom(12);key=Scrypt(salt=salt,length=32,n=32768,r=8,p=1).derive(password.encode('utf-8'))
    return {'version':1,'salt':encode(salt),'nonce':encode(nonce),'ciphertext':encode(AESGCM(key).encrypt(nonce,canonical(profile),b'agent-harness-profile-v1'))}
def decrypt_profile(data,password):
    if data.get('version')!=1: raise ValueError('Unsupported profile version')
    try:
        key=Scrypt(salt=decode(data['salt'],16),length=32,n=32768,r=8,p=1).derive(password.encode('utf-8'))
        return json.loads(AESGCM(key).decrypt(decode(data['nonce'],12),decode(data['ciphertext']),b'agent-harness-profile-v1'))
    except Exception: raise PermissionError('Unable to unlock local account') from None

def save_profile(path,profile,password):
    from common import write_json
    write_json(path,encrypt_profile(profile,password))
def load_profile(path,password): return decrypt_profile(json.loads(Path(path).read_text(encoding='utf-8-sig')),password)

def approve_certificate(identity,child_public_key,device_id=None):
    """Return the complete child delegation chain; device_id is UI metadata only."""
    return list(identity.get('chain',[]))+[certify_device(identity,child_public_key)]
def approve_delegate(identity,child_public_key,scopes):
    return list(identity.get('chain',[]))+[certify_delegate(identity,child_public_key,scopes)]
def encrypt_identity(identity,password): return encrypt_profile(identity,password)
def decrypt_identity(blob,password): return decrypt_profile(blob,password)
