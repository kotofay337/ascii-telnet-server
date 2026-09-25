rem # по умолчанию: telnet 23, дашборд 8080, бан на 10 минут
rem python3 ascii-telnet-server.py -f movie.dat

rem # дашборд на 127.0.0.1:9100, бан на 30 минут
rem python3 ascii-telnet-server.py -f movie.dat --http-interface 127.0.0.1 --http-port 9100 --ban-duration 1800

rem # отключить бан (оставить только мониторинг)
rem python3 ascii-telnet-server.py -f movie.dat --no-ban

rem # забанить вручную
rem curl "http://192.168.1.5:8080/api/ban?ip=10.0.0.42"

rem # снять бан
rem curl "http://192.168.1.5:8080/api/unban?ip=10.0.0.42"

rem # запустить с TTL закрытых клиентов в 5 секунд
rem python3 ascii-telnet-server.py -f movie.dat --closed-ttl 5

python ascii-telnet-server_3.14.6.py  --http-interface 192.168.0.13 --ban-duration 1800 --standalone -f sw1.txt

rem pause
call start.cmd
