from typing import Annotated

from beanie import Document, Indexed
from pydantic import EmailStr


class User(Document):
    # Review finding I1 (2026-09-10) — the address is the account.
    #
    # There was no index here at all, which cost this model twice over.
    #
    # Correctness first: registration read (`find_one`) and then wrote
    # (`insert`), and two signups for one address arriving together both
    # completed the read before either did the write. Both were told they
    # had created an account, both rows landed, and login's own `find_one`
    # afterwards returns whichever one Mongo feels like — so a person could
    # be handed the other person's password hash to authenticate against.
    # A uniqueness rule enforced in application code is not a uniqueness
    # rule; it has to be the database's job, because the database is the
    # only thing that sees both writers.
    #
    # Speed second: `find_one(User.email == ...)` runs on every single
    # authenticated request (get_current_user resolves the token's subject
    # through it) and was a full collection scan every time. The same index
    # fixes that for free.
    email: Annotated[EmailStr, Indexed(unique=True)]
    password_hash: str

    class Settings:
        name = "users"
