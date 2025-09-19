#!/usr/bin/env python3
"""
Simple Backpack Connector Test
"""
import sys
import os

# Add hummingbot path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'hummingbot'))

def test_imports():
    """Test basic imports"""
    print("Testing imports...")
    
    try:
        # Test constants
        from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants_simple import EXCHANGE_NAME
        print(f"Constants OK - Exchange: {EXCHANGE_NAME}")
        
        # Test PyNaCl
        import nacl.signing
        print("PyNaCl OK")
        
        # Test async-timeout
        import async_timeout
        print("async-timeout OK")
        
        return True
    except Exception as e:
        print(f"Import failed: {e}")
        return False

def test_crypto():
    """Test crypto functionality"""
    print("Testing crypto...")
    
    try:
        from nacl.signing import SigningKey
        from nacl.encoding import RawEncoder
        
        # Generate keypair
        private_key = SigningKey.generate()
        public_key = private_key.verify_key
        
        # Sign message
        message = b"test_message"
        signed = private_key.sign(message, encoder=RawEncoder)
        
        # Verify
        public_key.verify(message, signed.signature)
        print("Crypto test OK")
        return True
    except Exception as e:
        print(f"Crypto test failed: {e}")
        return False

def main():
    print("=== Backpack Connector Simple Test ===")
    
    tests = [
        ("Imports", test_imports),
        ("Crypto", test_crypto),
    ]
    
    passed = 0
    for name, test_func in tests:
        print(f"\n[{name}]")
        if test_func():
            passed += 1
            print("PASS")
        else:
            print("FAIL")
    
    print(f"\nResults: {passed}/{len(tests)} tests passed")
    
    if passed == len(tests):
        print("SUCCESS: All tests passed!")
        return True
    else:
        print("FAILURE: Some tests failed!")
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)