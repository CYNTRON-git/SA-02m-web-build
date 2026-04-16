#!/bin/bash
echo "Content-type: text/html"
echo "Set-Cookie: session_token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly"
echo "Location: /login.html"
echo ""
