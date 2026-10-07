"""Print a new key for BOOKS_TOKEN_KEY:  python -m books.keygen"""

from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
