class AppMessages:
    # Auth
    MISSING_USER_ID_HEADER = "Missing {header} header"
    MISSING_AUTH_HEADER = "Missing Authorization header"
    AUTH_IDENTITY_MISMATCH = "Unauthorized: X-USER-ID does not match JWT subject claim"
    AUTH_USER_NOT_FOUND = "Unauthorized: User not found or not authorised"
    INVALID_TOKEN_MISSING_SUB = "Invalid token: missing subject claim"
    TOKEN_EXPIRED = "Token has expired"
    TOKEN_MISSING_IAT = "Invalid token: missing issuance time (iat) claim"
    TOKEN_IAT_IN_FUTURE = "Invalid token: issuance time is in the future"
    TOKEN_INVALID_SIGNATURE = "Invalid token: Signature verification failed"
    TOKEN_INVALID_FORMAT = "Invalid token: {error}"
    INTERNAL_AUTH_ERROR = "Internal authentication error"

    # Documents
    FILE_ALREADY_UPLOADED = "A file named '{file_name}' has already been uploaded. Please rename the file and try again"
    DOC_METADATA_SAVE_FAILED = "Failed to record document metadata."
    DOC_NOT_FOUND = "Document '{doc_id}' not found."
    USER_NOT_FOUND = "User not found."
    FILE_SIZE_EXCEEDS_LIMIT = "File size exceeds the maximum limit of {max_size} MB."
    UNSUPPORTED_FILE_TYPE = "Unsupported file type '{extension}'. Allowed types: {allowed}."

    # Access code
    ACCESS_CODE_INVALID = "Invalid access code"


messages = AppMessages()
