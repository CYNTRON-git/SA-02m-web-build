#!/bin/bash
echo "Content-type: text/html; charset=utf-8"
echo "Set-Cookie: session_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/;"
echo "Location: /cgi-bin/index.cgi"
echo ""
