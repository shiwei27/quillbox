import win32com.client
try:
    ol = win32com.client.Dispatch("Outlook.Application")
    ns = ol.GetNamespace("MAPI")
    print("accounts:", ns.Accounts.Count)
    for i in range(1, ns.Accounts.Count+1):
        print(" -", ns.Accounts.Item(i).DisplayName)
except Exception as e:
    print("ERROR:", e)
