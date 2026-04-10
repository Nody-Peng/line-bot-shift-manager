import sheets
try:
    print("Connecting to 'users' sheet...")
    user = sheets.get_user("test_id")
    print("Connection successful! User:", user)
except Exception as e:
    print("Connection failed:", e)
