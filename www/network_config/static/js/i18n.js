/* SA-02m Web UI i18n runtime.
 * Keeps Russian as the source UI language and translates visible text dynamically.
 */
(function () {
  'use strict';

  const LANG_KEY = 'sa02m-lang';
  const RU = 'ru';
  const EN = 'en';
  let currentLang = RU;
  let applying = false;
  let observer = null;

  const textOriginal = new WeakMap();
  const attrOriginal = new WeakMap();

  const DICT = {
    'СА-02м — Панель управления': 'SA-02m Control Panel',
    'ЦИНТРОН': 'CYNTRON',
    'Версия веб-интерфейса': 'Web interface version',
    'Сервер автоматизации СА-02м': 'Automation Server SA-02m',
    'Сервер автоматизации СА-02м-2': 'Automation Server SA-02m-2',
    'Переключение темы: тёмная или светлая': 'Theme switch: dark or light',
    'Переключатель темы: тёмная или светлая': 'Theme switch: dark or light',
    'Светлая': 'Light',
    'Тёмная': 'Dark',
    'Темная': 'Dark',
    'Выход': 'Logout',
    'Сеть': 'Network',
    'Время': 'Time',
    'Устройства RS-485': 'RS-485 devices',
    'Шлюз RS-485': 'RS-485 Gateway',
    'Шлюз RS-485 → Ethernet': 'RS-485 Gateway → Ethernet',
    'Преобразователь интерфейсов RS-485/TCP: Modbus TCP, RTU over TCP, прозрачный режим': 'RS-485/TCP interface converter: Modbus TCP, RTU over TCP, transparent mode',
    'Шлюз': 'Gateway',
    'Управление': 'Management',
    'Система': 'System',
    'Загрузка CPU': 'CPU Load',
    'Загрузка ядер процессора': 'CPU core utilization',
    'Нагрузка (load avg)': 'Load Average',
    '1 мин': '1 min',
    '5 мин': '5 min',
    '15 мин': '15 min',
    'МГц': 'MHz',
    'ШИНА I2C ЗАНЯТА ДРУГОЙ СЛУЖБОЙ': 'I2C BUS IS BUSY WITH ANOTHER SERVICE',
    'НЕТ СВЯЗИ С МИКРОСХЕМОЙ РАСШИРЕНИЯ I2C': 'NO CONNECTION TO I2C EXPANDER CHIP',
    'Каналы не заданы — отредактируйте /etc/sa02m_hw.conf': 'Channels are not configured — edit /etc/sa02m_hw.conf',
    'Нет storage-mount — выполните установку системы (install.sh)': 'storage-mount is not installed — run the system installer (install.sh)',
    'Нажмите, чтобы переключить: при выкл. раздел без ФС или NTFS не форматируется': 'Click to toggle: when off, partitions without a filesystem or NTFS are not formatted',
    'Температура CPU': 'CPU Temperature',
    'Температура в норме': 'Temperature is normal',
    'Температура выше нормы': 'Temperature is above normal',
    'ОЗУ': 'RAM',
    'из —': 'of —',
    'из': 'of',
    'свободно': 'free',
    'Б': 'B',
    '<1м': '<1 min',
    'Применено': 'Applied',
    'Сброс USB уже выполняется': 'USB reset is already in progress',
    'Канал не настроен в /etc/sa02m_hw.conf': 'Channel is not configured in /etc/sa02m_hw.conf',
    'Шина I2C занята другой службой': 'I2C bus is busy with another service',
    'На устройстве нет i2c-tools': 'i2c-tools is not installed on the device',
    'Диск eMMC': 'eMMC Disk',
    'Службы': 'Services',
    'USB-накопитель': 'USB Storage',
    'USB-модем': 'USB Modem',
    'Дискретный выход, USB-питание и индикация': 'Discrete Output, USB Power, and Indication',
    'DO (дискретный выход)': 'DO (Discrete Output)',
    'Пищалка': 'Buzzer',
    'Аварийный LED (красный)': 'Alarm LED (red)',
    'Питание USB': 'USB Power',
    'Выкл': 'Off',
    'Вкл': 'On',
    'ВЫКЛ': 'OFF',
    'ВКЛ': 'ON',
    'Тихо': 'Silent',
    'Звук': 'Sound',
    'Сброс': 'Reset',
    'Стоп': 'Stop',
    'н/д': 'N/A',
    'НЕ УСТАНОВЛЕН': 'NOT MOUNTED',
    'Интерфейсы RS-485 — активность': 'RS-485 Interfaces — Activity',
    'Настройки сети': 'Network Settings',
    'Конфигурация Ethernet № 1': 'Ethernet #1 Configuration',
    'Конфигурация Ethernet № 1 и № 2': 'Ethernet #1 and #2 Configuration',
    'Статический IP-адрес': 'Static IP Address',
    'Статический': 'Static',
    'DHCP:': 'DHCP:',
    'Статический адрес Ethernet № 1': 'Static Ethernet #1 Address',
    'Статический адрес Ethernet № 2': 'Static Ethernet #2 Address',
    'IP-адрес': 'IP Address',
    'Маска подсети': 'Subnet Mask',
    'Шлюз': 'Gateway',
    'DNS-серверы': 'DNS Servers',
    'Применить': 'Apply',
    'Дата и время': 'Date and Time',
    'Временая зона и синхронизация системных часов': 'Time zone and system clock synchronization',
    'Текущее время на устройстве': 'Current device time',
    'Текущее время': 'Current time',
    'Время с RTC': 'RTC time',
    'Часовой пояс': 'Time Zone',
    'Москва, Санкт-Петербург (UTC+3)': 'Moscow, Saint Petersburg (UTC+3)',
    'Самара, Ижевск (UTC+4)': 'Samara, Izhevsk (UTC+4)',
    'Екатеринбург, Челябинск (UTC+5)': 'Yekaterinburg, Chelyabinsk (UTC+5)',
    'Омск (UTC+6)': 'Omsk (UTC+6)',
    'Красноярск, Новосибирск (UTC+7)': 'Krasnoyarsk, Novosibirsk (UTC+7)',
    'Иркутск (UTC+8)': 'Irkutsk (UTC+8)',
    'Якутск (UTC+9)': 'Yakutsk (UTC+9)',
    'Владивосток, Хабаровск (UTC+10)': 'Vladivostok, Khabarovsk (UTC+10)',
    'Магадан, Сахалин (UTC+11)': 'Magadan, Sakhalin (UTC+11)',
    'Камчатка, Чукотка (UTC+12)': 'Kamchatka, Chukotka (UTC+12)',
    'Калининград (UTC+2)': 'Kaliningrad (UTC+2)',
    'Всемирное время (UTC±0)': 'Coordinated Universal Time (UTC±0)',
    'Дата и время (ГГГГ-ММ-ДД ЧЧ:ММ:СС)': 'Date and Time (YYYY-MM-DD HH:MM:SS)',
    'Оставьте пустым, чтобы не менять время': 'Leave empty to keep the current time',
    'Взять дату/время с этого ПК': 'Use Date/Time from This PC',
    'Синхронизировать с этим ПК': 'Synchronize with This PC',
    'Применить вручную': 'Apply Manually',
    'Аппаратный вариант': 'Hardware Variant',
    'Тип устройства': 'Device Type',
    'SA-02m (1 Ethernet, 5 COM-портов)': 'SA-02m (1 Ethernet, 5 COM ports)',
    'SA-02m-2 (2 Ethernet, 4 COM-порта)': 'SA-02m-2 (2 Ethernet, 4 COM ports)',
    'Управление системой': 'System Management',
    'Службы, перезагрузка и журнал событий': 'Services, reboot, and event log',
    'Доступ': 'Access',
    'Текущий пароль': 'Current Password',
    'Новый логин': 'New Login',
    'Новый пароль': 'New Password',
    'Повтор пароля': 'Repeat Password',
    'Сохранить': 'Save',
    'USB / microSD': 'USB / microSD',
    'Автоформат в exFAT': 'Automatic exFAT formatting',
    'Переключить автоформат при подключении носителя': 'Toggle automatic formatting when media is connected',
    'ВКЛЮЧЕНО': 'ENABLED',
    'ОТКЛЮЧЕНО': 'DISABLED',
    'НЕ УСТАНОВЛЕНО': 'NOT INSTALLED',
    'Перезапустить': 'Restart',
    'Перезагрузка устройства': 'Device Reboot',
    'Перезагрузить': 'Reboot',
    'Обновление веб': 'Web Update',
    'Задеплоено': 'Deployed',
    'На GitHub': 'On GitHub',
    'Проверка': 'Checked',
    'Проверить обновления': 'Check for Updates',
    'Применить обновление': 'Apply Update',
    'Загрузка…': 'Loading...',
    'Обновить список': 'Refresh List',
    'Журнал событий': 'Event Log',
    'Обновить': 'Refresh',
    'SSH-отладка': 'SSH Debug',
    'Экспорт журнала': 'Export Log',
    'Нажмите «Обновить» для загрузки журнала или «SSH-отладка» для диагностики доступа по SSH': 'Click Refresh to load the log, or SSH Debug to diagnose SSH access.',
    'Поиск модулей на линии RS-485, настройка и обновление прошивки': 'Scan modules on the RS-485 line, configure devices, and update firmware',
    'Найденные устройства': 'Detected Devices',
    'Адрес': 'Address',
    'Сигнатура': 'Signature',
    'Прошивка': 'Firmware',
    'Бутлоадер': 'Bootloader',
    'Скорость': 'Baud Rate',
    'Нажмите «Сканировать», чтобы найти устройства на выбранной линии RS-485.': 'Click Scan to find devices on the selected RS-485 line.',
    'Журнал операции': 'Operation Log',
    'Ожидание операции…': 'Waiting for an operation...',
    'Параметры поиска': 'Scan Parameters',
    'Выберите линию': 'Select Line',
    'Обновить список портов': 'Refresh port list',
    'Скорости': 'Baud Rates',
    'Можно выбрать сразу несколько скоростей.': 'Multiple baud rates can be selected.',
    'Режим поиска': 'Scan Mode',
    'Подготовка порта': 'Preparing port',
    'Сканирование': 'Scanning',
    'Поиск': 'Searching',
    'Стандартный (адресный)': 'Standard (addressed)',
    'Быстрый Modbus (Арбитраж)': 'Fast Modbus (arbitration)',
    'Диапазон адресов': 'Address Range',
    'Сканировать': 'Scan',
    'Отмена': 'Cancel',
    'Состояние линии': 'Line Status',
    'Нет данных': 'No Data',
    'Опрос не оценён': 'Polling Not Checked',
    'Выберите порт, чтобы увидеть состояние линии и опроса.': 'Select a port to view line and polling status.',
    'Остановить unit’ы опроса RS-485 (MPLC_STOP_SERVICES)': 'Stop RS-485 polling units (MPLC_STOP_SERVICES)',
    'Остановить опрос': 'Stop Polling',
    'Запустить обратно службы, остановленные вручную': 'Start services that were stopped manually',
    'Вернуть опрос': 'Restore Polling',
    'Прошивка выбранных устройств': 'Firmware for Selected Devices',
    'Обновить манифест и скачать последнюю stable-прошивку (и текущую с линии) в кеш шлюза': 'Refresh manifest and download the latest stable firmware, plus current line versions, to the gateway cache',
    'Скачать прошивки': 'Download Firmware',
    'Выбрать файл прошивки с устройства': 'Select a firmware file from this device',
    'Выбрать .fw': 'Select .fw',
    'Доступные прошивки': 'Available Firmware',
    'Манифест ещё не загружен': 'Manifest has not been loaded yet',
    'Нет доступа к интернету': 'No internet access',
    'Не удалось обновить список прошивок': 'Failed to refresh firmware list',
    'Не удалось загрузить список прошивок': 'Failed to load firmware list',
    'Не удалось загрузить прошивку': 'Failed to upload firmware',
    'Не удалось скачать прошивку с сервера': 'Failed to download firmware from server',
    'Некорректный ответ сервера прошивок': 'Invalid response from firmware server',
    'Некорректный формат списка прошивок': 'Invalid firmware list format',
    'Сервер прошивок временно недоступен': 'Firmware server temporarily unavailable',
    'Выберите корректную прошивку': 'Select compatible firmware',
    'Прошить': 'Flash',
    'Отменить': 'Cancel',
    'Настройка устройства': 'Device Configuration',
    'Закрыть окно настройки': 'Close configuration window',
    'Закрыть': 'Close',
    'MQTT': 'MQTT',
    'Конфигурация Modbus→MQTT моста и мониторинг топиков': 'Modbus-to-MQTT bridge configuration and topic monitoring',
    '1883 (локал.) · 1884 (внешний)': '1883 (local) · 1884 (external)',
    'Клиентов: —': 'Clients: —',
    'Мост —': 'Bridge —',
    'Работает': 'Running',
    'Остановлен': 'Stopped',
    'Мост активен': 'Bridge Active',
    'Мост остановлен': 'Bridge Stopped',
    'Нет доступа': 'Access Denied',
    'Остановить': 'Stop',
    '⟳ Перезапустить': '⟳ Restart',
    'Сохранить и применить': 'Save and Apply',
    'Подключение MQTT с ПК': 'MQTT Connection from PC',
    '1884 + пароль · 1883 только localhost': '1884 + password · 1883 localhost only',
    'Хост': 'Host',
    'Порт': 'Port',
    'Логин': 'Login',
    'Пароль': 'Password',
    'Топик': 'Topic',
    'Топик скопирован': 'Topic copied',
    'Нажмите, чтобы скопировать': 'Click to copy',
    'Нажмите, чтобы показать и скопировать': 'Click to reveal and copy',
    'Устройства на шине': 'Bus Devices',
    '🔍 Поиск': '🔍 Scan',
    '+ Вручную': '+ Manual',
    'Тип': 'Type',
    'Имя': 'Name',
    'Каналов': 'Channels',
    'Вх./вых.': 'I/O',
    'Каналы': 'Channels',
    'Монитор топиков': 'Topic Monitor',
    '— выберите устройство —': '— select device —',
    '⏸ Пауза': '⏸ Pause',
    '✕ Очистить': '✕ Clear',
    '🔍 Поиск устройств на шине': '🔍 Scan Bus Devices',
    'Адреса': 'Addresses',
    '1 – 247 (медл.)': '1 – 247 (slow)',
    'Добавить устройство': 'Add Device',
    'Тип устройства': 'Device Type',
    'Адрес Modbus': 'Modbus Address',
    'Имя устройства': 'Device Name',
    'Щит освещения': 'Lighting Panel',
    'Добавить': 'Add',
    'Отключён': 'Disabled',
    'Прозрачный': 'Transparent',
    'Нет (N)': 'None (N)',
    'Чётный (E)': 'Even (E)',
    'Нечётный (O)': 'Odd (O)',
    '● Сервис активен': '● Service Active',
    '● Сервис остановлен': '● Service Stopped',
    'Запустить': 'Start',
    '↺ Перезагрузить конфиг': '↺ Reload Config',
    '⟳ Перезапустить': '⟳ Restart',
    'Порты': 'Ports',
    'Режим': 'Mode',
    'Бод': 'Baud',
    'Статус': 'Status',
    'Режим и TCP': 'Mode and TCP',
    'Включить порт': 'Enable Port',
    'Режим работы': 'Operating Mode',
    'TCP-порт': 'TCP Port',
    'Отвечать локально на WB Fast Modbus probe': 'Reply locally to WB Fast Modbus probe',
    'Параметры RS-485': 'RS-485 Parameters',
    'Скорость (бод)': 'Baud Rate',
    'Чётность': 'Parity',
    'Стоп-биты': 'Stop Bits',
    'Биты данных': 'Data Bits',
    'Сбросить': 'Reset',
    'Сохранение…': 'Saving...',
    'Сохранено': 'Saved',
    'Отключён': 'Disabled',
    '● Работает': '● Running',
    '● Остановлен': '● Stopped',
    'Ошибок:': 'Errors:',
    'Нет портов в ответе демона (проверьте sa02m-flasher и /etc/sa02m_flasher.conf)': 'No ports in daemon response. Check sa02m-flasher and /etc/sa02m_flasher.conf.',
    'нет устройства': 'device missing',
    'активная задача': 'active job',
    'Нет задач в памяти демона. Запустите сканирование или прошивку — строки журнала появятся здесь.': 'No jobs in daemon memory. Start a scan or flash operation; log lines will appear here.',
    '(В снимке задачи нет сохранённых строк журнала)': '(The job snapshot contains no saved log lines)',
    'Выберите устройство': 'Select a device',
    'Нельзя прошивать вместе модули MR/MP и Wiren Board. Выберите устройства одного типа.': 'MR/MP modules and Wiren Board devices cannot be flashed together. Select devices of one type.',
    'Выберите файл прошивки из списка или загрузите .fw вручную.': 'Select firmware from the list or upload a .fw file manually.',
    'Выбранный образ не скачан в кеш шлюза. Нажмите «Скачать прошивки» или «Выбрать .fw».': 'The selected image is not downloaded to the gateway cache. Click Download Firmware or Select .fw.',
    'Нет линии': 'No Line',
    'Проверка порта': 'Checking Port',
    'Задача активна': 'Job Active',
    'Порт занят': 'Port Busy',
    'Порт свободен': 'Port Free',
    'Проверка опроса': 'Checking Polling',
    'Опрос активен': 'Polling Active',
    'Опрос освобождён': 'Polling Released',
    'Опрос не определён': 'Polling Unknown',
    'Опрос не активен': 'Polling Inactive',
    'Проверка порта и опроса': 'Checking port and polling',
    'На линии выполняется активная задача, дождитесь её завершения.': 'An active job is running on the line; wait for it to complete.',
    'Устройство порта не найдено в системе.': 'Port device was not found in the system.',
    'Линия готова к сканированию и прошивке.': 'The line is ready for scanning and flashing.',
    'Прошивки не найдены. Нажмите «Проверить» или выберите .fw вручную.': 'No firmware images found. Click Check or select a .fw file manually.',
    'все варианты MR-02м (общий образ)': 'all MR-02m variants (common image)',
    'не скачан': 'not downloaded',
    'Устройств не найдено.': 'No devices found.',
    'Датчик Sens / DTV-RS-485': 'Sens / DTV-RS-485 Sensor',
    'Анализатор сети CE-02м-3': 'CE-02m-3 Power Analyzer',
    'Модуль MR/MP-02м': 'MR/MP-02m Module',
    'есть': 'available',
    'Сведения': 'Overview',
    'Измерения': 'Measurements',
    'Настройки': 'Settings',
    'ТТ и фазы': 'CT and Phases',
    'Реле и задержки': 'Relays and Delays',
    'Дискретный вход': 'Discrete Input',
    'Дискретный выход': 'Discrete Output',
    'Аналоговый выход': 'Analog Output',
    'Аналоговый вход': 'Analog Input',
    'Загрузка настроек…': 'Loading settings...',
    'Настройка датчика': 'Sensor Configuration',
    'Настройка анализатора сети': 'Power Analyzer Configuration',
    'Настройка модуля расширения': 'Expansion Module Configuration',
    'Параметры сети сохранены': 'Network parameters saved',
    'Время работы': 'Uptime',
    'Серийный номер': 'Serial Number',
    'Температура МК': 'MCU Temperature',
    'Питание МК': 'MCU Supply',
    'Наработка': 'Operating Time',
    'ОЗУ свободно': 'Free RAM',
    'ОЗУ занято': 'Used RAM',
    'Причина перезагрузки': 'Reset Reason',
    'Обновлений прошивки': 'Firmware Updates',
    'Питание': 'Supply',
    'Величина:': 'Quantity:',
    'Единица:': 'Unit:',
    'Состояние': 'State',
    'Активен': 'Active',
    'Неактивен': 'Inactive',
    'Режим измерения частоты': 'Frequency measurement mode',
    'Выключен': 'Disabled',
    'Задержка вкл.': 'On delay',
    'Задержка выкл.': 'Off delay',
    'Пульс при вкл.': 'Pulse on activation',
    'Пульс при выкл.': 'Pulse on deactivation',
    'Мигание': 'Blinking',
    'Кнопка': 'Button',
    'Счетчик импульсов': 'Pulse counter',
    'Сопротивление': 'Resistance',
    'Напряжение': 'Voltage',
    'Ток': 'Current',
    'Логический вход': 'Logic input',
    'Термопара': 'Thermocouple',
    'Сухой контакт': 'Dry Contact',
    'Темп. внешний': 'External Temp.',
    'Присутствие': 'Presence',
    'Дистанция движения': 'Movement Distance',
    'Освещённость': 'Illuminance',
    'Зуммер': 'Buzzer',
    'Светодиоды': 'LEDs',
    'Порт свободен, опрос не выполняется': 'Port is free; polling is not running',
    'Интерфейс отсутствует': 'Interface is absent',
    'Опрос активен, ошибки линии (FE/PE/OE)': 'Polling active; line errors (FE/PE/OE)',
    'Опрос активен, ответы устройств в норме': 'Polling active; device responses are normal',
    'Опрос активен, нет ответов устройств': 'Polling active; no device responses',
    'нет опроса': 'no polling',
    'статистика отключена': 'statistics disabled',
    'Статистика RS-485 отключена (sa02m_status_blocks.conf)': 'RS-485 statistics disabled (sa02m_status_blocks.conf)',
    'Нечего копировать': 'Nothing to copy',
    'Скопировано:': 'Copied:',
    'Не удалось скопировать': 'Copy failed',
    'Пароль скопирован': 'Password copied',
    'Не удалось скопировать пароль': 'Password copy failed',
    'Запустить мост Modbus→MQTT': 'Start Modbus-to-MQTT bridge',
    'Остановить мост Modbus→MQTT и освободить COM-порты': 'Stop Modbus-to-MQTT bridge and release COM ports',
  };

  const REGEX = [
    [/^из (.+)$/u, 'of $1'],
    [/^свободно (.+)$/u, '$1 free'],
    [/^Процессов: (.+)$/u, 'Processes: $1'],
    [/^Ядро: (.+)$/u, 'Kernel: $1'],
    [/^R (.+) \/ W (.+)$/u, 'R $1 / W $2'],
    [/^Линк$/u, 'Link'],
    [/^Нет линка$/u, 'No Link'],
    [/^Обновление основных виджетов приостановлено: несколько таймаутов ответа status\.cgi\. Проверьте нагрузку и nginx\/fastcgi\.$/u, 'Main widget updates paused: multiple status.cgi response timeouts. Check load and nginx/fastcgi.'],
    [/^Обновление основных виджетов приостановлено из-за ошибок ответа status\.cgi\.$/u, 'Main widget updates paused due to status.cgi response errors.'],
    [/^Есть обновление\.?$/u, 'Update available.'],
    [/^Обновлений нет\.?$/u, 'No updates available.'],
    [/^Проверка не удалась\.?$/u, 'Update check failed.'],
    [/^Проверка не удалась: (.+)$/u, 'Update check failed: $1'],
    [/^Проверка выполнена$/u, 'Check completed'],
    [/^Сессия истекла\.?$/u, 'Session expired.'],
    [/^Применяется…$/u, 'Applying...'],
    [/^Загрузка и установка обновления…$/u, 'Downloading and installing update...'],
    [/^Нет ответа от сервера$/u, 'No response from server'],
    [/^Обновление применено\. Страница перезагрузится через 5 секунд…$/u, 'Update applied. The page will reload in 5 seconds...'],
    [/^Обновление применено успешно$/u, 'Update applied successfully'],
    [/^Ошибка обновления\. Проверьте лог ниже\.$/u, 'Update failed. Check the log below.'],
    [/^Ошибка обновления$/u, 'Update failed'],
    [/^Таймаут — повторите$/u, 'Timeout. Try again.'],
    [/^Таймаут запроса — интерфейс не завис, повторите$/u, 'Request timeout. The interface is still responsive; try again.'],
    [/^Нет связи с сервером$/u, 'No connection to server'],
    [/^Ошибка: (.+)$/u, 'Error: $1'],
    [/^Сетевая ошибка: (.+)$/u, 'Network error: $1'],
    [/^HTTP (.+)$/u, 'HTTP $1'],
    [/^Команда «(.+)» выполнена$/u, 'Command "$1" completed'],
    [/^(.+) сохранён$/u, '$1 saved'],
    [/^Включено портов: (\d+) \/ (\d+)\s+\|\s+Работают: (\d+)$/u, 'Enabled ports: $1 / $2 | Running: $3'],
    [/^Клиентов: (.+)$/u, 'Clients: $1'],
    [/^Порты: (.+)$/u, 'Ports: $1'],
    [/^Манифест: (.+)$/u, 'Manifest: $1'],
    [/^Не удалось скачать приложения v(.+)$/u, 'Failed to download application v$1'],
    [/^Не удалось скачать бутлоадера v(.+)$/u, 'Failed to download bootloader v$1'],
    [/^Сервер прошивок отклонил запрос \((.+)\)$/u, 'Firmware server rejected the request ($1)'],
    [/^Сервер прошивок временно недоступен \((.+)\)$/u, 'Firmware server temporarily unavailable ($1)'],
    [/^Ошибка сервера \((.+)\)$/u, 'Server error ($1)'],
    [/^Загрузка прошивки: (.+)$/u, 'Firmware upload: $1'],
    [/^Загружено: (.+)$/u, 'Uploaded: $1'],
    [/^Список прошивок обновлён \(записей: (.+)\)$/u, 'Firmware list updated (entries: $1)'],
    [/^Список прошивок обновлён \(записей: (.+)\), очищено из кеша: (.+)$/u, 'Firmware list updated (entries: $1), purged from cache: $2'],
    [/^Последняя задача: (.+), порт (.+), состояние (.+)$/u, 'Last job: $1, port $2, state $3'],
    [/^Не удалось загрузить журнал с сервера: (.+)$/u, 'Failed to load log from server: $1'],
    [/^Сигнатура не распознана: (.+)\. Выполните сканирование\.$/u, 'Signature not recognized: $1. Run a scan.'],
    [/^Сигнатура «(.+)» не распознана\. Выполните сканирование\.$/u, 'Signature "$1" is not recognized. Run a scan.'],
    [/^Для модуля MR\/MP-02m \(«(.+)»\) выберите прошивку \.fw, не \.wbfw\.$/u, 'For MR/MP-02m module "$1", select .fw firmware, not .wbfw.'],
    [/^Для устройства «(.+)» \(Wiren Board\) выберите прошивку \.wbfw\.$/u, 'For Wiren Board device "$1", select .wbfw firmware.'],
    [/^Линию сейчас опрашивают: (.+)\. При сканировании опрос будет остановлен автоматически; кнопка «Остановить опрос» делает это вручную\.$/u, 'The line is currently polled by: $1. Polling will be stopped automatically during scanning; Stop Polling does it manually.'],
    [/^Опрос вручную освобождён: (.+)\.$/u, 'Polling manually released: $1.'],
    [/^Порт удерживают PID (.+)\. Если это не служба опроса, освободите процесс вручную\.$/u, 'Port is held by PID $1. If this is not a polling service, release the process manually.'],
    [/^Порт удерживают PID (.+)\. systemd не сообщает об активном unit опроса — порт может держать другой процесс; при необходимости проверьте systemctl status и fuser на устройстве\.$/u, 'Port is held by PID $1. systemd reports no active polling unit; another process may hold the port. Check systemctl status and fuser on the device if needed.'],
    [/^есть (.+)$/u, 'available $1'],
    [/^\(не скачан — «Скачать прошивки»\)$/u, '(not downloaded: use Download Firmware)'],
    [/^(.+) адр\. · (.+)$/u, '$1 addr · $2'],
    [/^Не удалось загрузить настройки: (.+)$/u, 'Failed to load settings: $1'],
    [/^Настройка устройства: (.+)$/u, 'Device configuration: $1'],
    [/^Сохранение сети: (.+)$/u, 'Saving network: $1'],
    [/^Сеть устройства: (.+)$/u, 'Device network: $1'],
    [/^Дождитесь окончания сканирования или прошивки$/u, 'Wait until scanning or flashing is complete'],
    [/^Дата и время подставлены с этого ПК\. При необходимости нажмите «Применить вручную»\.$/u, 'Date and time were filled from this PC. Click Apply Manually if needed.'],
    [/^Время синхронизировано с этим ПК$/u, 'Time synchronized with this PC'],
    [/^Не удалось установить время с этого ПК\.$/u, 'Failed to set time from this PC.'],
    [/^Время синхронизировано; таймзона не изменилась\.$/u, 'Time synchronized; time zone was not changed.'],
    [/^(.+) \(этот ПК\)$/u, '$1 (this PC)'],
    [/^Сохранено\. При следующем входе используйте новый логин и пароль\.$/u, 'Saved. Use the new login and password at the next sign-in.'],
    [/^Неверный текущий пароль$/u, 'Incorrect current password'],
    [/^Новый пароль и повтор не совпадают$/u, 'New password and confirmation do not match'],
    [/^Недопустимый логин \(латиница, цифры, \. _ - , до 32 символов\)$/u, 'Invalid login (Latin letters, digits, . _ -, up to 32 characters)'],
    [/^Длина пароля 4–128 символов$/u, 'Password length must be 4-128 characters'],
    [/^Пароль не может содержать символы (.+)$/u, 'Password cannot contain characters $1'],
    [/^Укажите новый пароль$/u, 'Enter a new password'],
    [/^Укажите логин$/u, 'Enter a login'],
    [/^Укажите текущий пароль$/u, 'Enter the current password'],
    [/^Файл учётных данных на устройстве недоступен$/u, 'Credential file is unavailable on the device'],
    [/^Не удалось сохранить настройки$/u, 'Failed to save settings'],
    [/^(.+) COM-порт$/u, '$1 COM port'],
    [/^(.+) COM-порта$/u, '$1 COM ports'],
    [/^(.+) COM-портов$/u, '$1 COM ports'],
    [/^✓ Применено: (.+), (.+)$/u, '✓ Applied: $1, $2'],
    [/^✗ Ошибка: (.+)$/u, '✗ Error: $1'],
    [/^Настройки Ethernet № 1 применены\. Перезагрузите сеть\.$/u, 'Ethernet #1 settings applied. Restart networking.'],
    [/^Настройки Ethernet № 2 применены\.$/u, 'Ethernet #2 settings applied.'],
    [/^Укажите IP и маску для Ethernet № 1$/u, 'Enter IP and subnet mask for Ethernet #1'],
    [/^Укажите IP для Ethernet № 2$/u, 'Enter IP for Ethernet #2'],
    [/^Время\/таймзона применены$/u, 'Time/time zone applied'],
    [/^Ошибка сервера: (.+)$/u, 'Server error: $1'],
    [/^Не удалось установить время\. Проверьте формат и \/var\/log\/sa02m_install\.log на устройстве\.$/u, 'Failed to set time. Check the format and /var/log/sa02m_install.log on the device.'],
    [/^Таймзона не применена\.$/u, 'Time zone was not applied.'],
    [/^Настройки применены; таймзона не изменилась\.$/u, 'Settings applied; time zone was not changed.'],
    [/^Ошибка отправки$/u, 'Submit failed'],
    [/^Нет управляемых служб$/u, 'No manageable services'],
    [/^Список служб обновлён$/u, 'Service list refreshed'],
    [/^Службы: (.+)$/u, 'Services: $1'],
    [/^Служба остановлена и отключена$/u, 'Service stopped and disabled'],
    [/^Служба включена$/u, 'Service enabled'],
    [/^Служба: (.+)$/u, 'Service: $1'],
    [/^Перезапустить службы nginx и fcgiwrap\?$/u, 'Restart nginx and fcgiwrap services?'],
    [/^Перезагрузить контроллер\?$/u, 'Reboot the controller?'],
    [/^Команда перезапуска отправлена\. Если systemd недоступен, смотрите \/var\/log\/sa02m_install\.log$/u, 'Restart command sent. If systemd is unavailable, check /var/log/sa02m_install.log.'],
    [/^Перезапуск служб: (.+)$/u, 'Service restart: $1'],
    [/^Перезагрузка… страница обновится через 60 с$/u, 'Rebooting... page will reload in 60 s'],
    [/^Перезагрузка не запущена: (.+)$/u, 'Reboot was not started: $1'],
    [/^Не удалось загрузить журнал$/u, 'Failed to load log'],
    [/^Загрузка SSH-диагностики… \(до ~2 мин\)$/u, 'Loading SSH diagnostics... (up to ~2 min)'],
    [/^SSH-диагностика$/u, 'SSH Diagnostics'],
    [/^Не удалось загрузить SSH-диагностику$/u, 'Failed to load SSH diagnostics'],
    [/^Настройки применены$/u, 'Settings applied'],
    [/^Время применено; таймзона не изменилась$/u, 'Time applied; time zone was not changed'],
    [/^Ошибка: неверная таймзона$/u, 'Error: invalid time zone'],
    [/^Ошибка: не удалось установить время$/u, 'Error: failed to set time'],
    [/^Службы перезапущены$/u, 'Services restarted'],
    [/^Перезагрузка запущена…$/u, 'Reboot started...'],
    [/^Сброс питания на (\d+) сек$/u, 'Power reset for $1 s'],
    [/^Статус: (.+)$/u, 'Status: $1'],
    [/^Подключён$/u, 'Connected'],
    [/^Нет сети$/u, 'No Network'],
    [/^↓ (.+) \/ ↑ (.+)$/u, '↓ $1 / ↑ $2'],
    [/^Ош FE=(.+) PE=(.+) OE=(.+)$/u, 'Err FE=$1 PE=$2 OE=$3'],
    [/^Ош (.+)$/u, 'Err $1'],
    [/^RS-485-(\d+) \(COM(\d+)\)$/u, 'RS-485-$1 (COM$2)'],
    [/^вкл$/u, 'on'],
    [/^выкл$/u, 'off'],
    [/^байт$/u, 'bytes'],
    [/^КБ$/u, 'kB'],
    [/^МБ$/u, 'MB'],
    [/^ГБ$/u, 'GB'],
    [/^(\d+(?:\.\d+)?) kB$/u, '$1 kB'],
    [/^bootloader модулей расширения МР-02м · (.+) · stable$/u, 'MR-02m expansion module bootloader · $1 · stable'],
    [/^bootloader датчиков температуры и влажности ДТВ-RS-485 · (.+) · stable$/u, 'DTV-RS-485 temperature/humidity sensor bootloader · $1 · stable'],
    [/^Модули расширения МР-02м · (.+) · stable( · не скачан)?$/u, 'MR-02m expansion modules · $1 · stable$2'],
    [/^Датчики температуры и влажности ДТВ-RS-485 · (.+) · stable · не скачан$/u, 'DTV-RS-485 temperature/humidity sensors · $1 · stable · not downloaded'],
    [/^Датчики температуры и влажности ДТВ-RS-485 · (.+) · stable$/u, 'DTV-RS-485 temperature/humidity sensors · $1 · stable'],
    [/^д$/u, 'd'],
    [/^ч$/u, 'h'],
    [/^м$/u, 'm'],
    [/^с$/u, 's'],
    [/^Старт сканирования на (.+)$/u, 'Scan started on $1'],
    [/^Скан (.+) \((.+)\), быстрый [Mm]odbus$/u, 'Scanning $1 ($2), fast Modbus'],
    [/^Скан (.+) \((.+)\), быстрый модбас$/u, 'Scanning $1 ($2), fast Modbus'],
    [/^\[(\d+) N(\d+)\] адр\.(\d+) опрошен, SN (.+) \((.+)\)$/u, '[$1 N$2] addr.$3 polled, SN $4 ($5)'],
    [/^\[(\d+) N(\d+)\] адр\.(\d+) опрошен, SN (.+)$/u, '[$1 N$2] addr.$3 polled, SN $4'],
    [/^Найдено устройств: (\d+)$/u, 'Devices found: $1'],
    [/^Сканирование завершено\. Найдено (\d+) устройств\.$/u, 'Scan complete. Devices found: $1'],
    [/^Сканирование завершилось с ошибкой$/u, 'Scan failed'],
    [/^Сканирование отменено$/u, 'Scan cancelled'],
    [/^Сканирование: (.+)$/u, 'Scanning: $1'],
    [/^Сканирование уже выполняется$/u, 'Scan already in progress'],
    [/^Поиск на (\d+)$/u, 'Searching at $1'],
    [/^Адрес (\d+), (\d+)$/u, 'Address $1, $2'],
    [/^Адрес (\d+)$/u, 'Address $1'],
    [/^Опрос адреса (\d+)$/u, 'Polling address $1'],
    [/^Старт сканирования$/u, 'Scan started'],
  ];

  function storedLang() {
    try {
      const lang = localStorage.getItem(LANG_KEY);
      return lang === EN || lang === RU ? lang : RU;
    } catch (_) {
      return RU;
    }
  }

  function saveLang(lang) {
    try { localStorage.setItem(LANG_KEY, lang); } catch (_) {}
  }

  function normalize(s) {
    return String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  }

  function translateText(src) {
    const raw = String(src == null ? '' : src);
    const lead = raw.match(/^\s*/)[0];
    const trail = raw.match(/\s*$/)[0];
    const key = normalize(raw);
    if (!key) return raw;
    if (DICT[key]) return lead + DICT[key] + trail;
    for (const rule of REGEX) {
      if (rule[0].test(key)) return lead + key.replace(rule[0], rule[1]) + trail;
    }
    return raw;
  }

  function attrMapFor(el) {
    let map = attrOriginal.get(el);
    if (!map) {
      map = {};
      attrOriginal.set(el, map);
    }
    return map;
  }

  function translateAttr(el, attr, refreshOriginal) {
    if (!el.hasAttribute(attr)) return;
    const map = attrMapFor(el);
    if (refreshOriginal || map[attr] == null) map[attr] = el.getAttribute(attr);
    const next = currentLang === EN ? translateText(map[attr]) : map[attr];
    if (el.getAttribute(attr) !== next) el.setAttribute(attr, next);
  }

  function translateTextNode(node) {
    if (!textOriginal.has(node)) textOriginal.set(node, node.nodeValue);
    const original = textOriginal.get(node);
    const next = currentLang === EN ? translateText(original) : original;
    if (node.nodeValue !== next) node.nodeValue = next;
  }

  function isI18nControl(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
    const id = node.id;
    return id === 'lang-toggle' || id === 'theme-toggle'
      || id === 'lang-toggle-label' || id === 'theme-toggle-label' || id === 'theme-toggle-icon'
      || id === 'topbar-user-name'
      || id === 'time-sys-disp' || id === 'time-rtc-disp';
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      translateTextNode(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
    const el = root.nodeType === Node.ELEMENT_NODE ? root : null;
    if (el) {
      if (/^(SCRIPT|STYLE|TEXTAREA)$/i.test(el.tagName)) return;
      if (isI18nControl(el)) return;
      ['title', 'aria-label', 'placeholder', 'alt'].forEach(attr => translateAttr(el, attr));
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
          if (/^(SCRIPT|STYLE|TEXTAREA)$/i.test(node.tagName)) return NodeFilter.FILTER_REJECT;
          if (isI18nControl(node)) return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    let node = walker.currentNode;
    while (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        translateTextNode(node);
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        ['title', 'aria-label', 'placeholder', 'alt'].forEach(attr => translateAttr(node, attr));
      }
      node = walker.nextNode();
    }
  }

  function applyDataI18n() {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      const key = el.dataset.i18n;
      if (!key) return;
      el.textContent = currentLang === EN ? translateText(key) : key;
    });
  }

  function updateControl() {
    document.documentElement.lang = currentLang;
    const label = document.getElementById('lang-toggle-label');
    if (label) label.textContent = currentLang === EN ? 'Русский' : 'English';
    const btn = document.getElementById('lang-toggle');
    if (btn) {
      btn.title = currentLang === EN ? 'Switch to Russian / Переключить на русский' : 'Switch to English / Переключить на английский';
      btn.setAttribute('aria-label', currentLang === EN ? 'Switch to Russian' : 'Switch to English');
    }
    document.title = currentLang === EN ? translateText('СА-02м — Панель управления') : 'СА-02м — Панель управления';
    if (typeof window.applyDeviceTitle === 'function') window.applyDeviceTitle();
    applyDataI18n();
    if (typeof window.updateThemeBtn === 'function') window.updateThemeBtn();
    if (typeof window.refreshServicesControlI18n === 'function') window.refreshServicesControlI18n();
    if (typeof window.refreshServicesDynamicI18n === 'function') window.refreshServicesDynamicI18n();
    if (typeof window.refreshMainStatusI18n === 'function') window.refreshMainStatusI18n();
    if (typeof window.refreshPriorityStatusI18n === 'function') window.refreshPriorityStatusI18n();
    if (typeof window.refreshRs485I18n === 'function') window.refreshRs485I18n();
    if (typeof window.mqttRefreshI18n === 'function') window.mqttRefreshI18n();
    if (typeof window.flasherRerenderLog === 'function') window.flasherRerenderLog();
    if (typeof window.flasherRerenderScanStatus === 'function') window.flasherRerenderScanStatus();
    if (typeof window.hwRerenderBlockStatus === 'function') window.hwRerenderBlockStatus();
    if (typeof window.flasherRerenderProgress === 'function') window.flasherRerenderProgress();
    const userName = document.getElementById('topbar-user-name');
    if (userName) userName.textContent = 'admin';
  }

  function pauseObserver() {
    if (observer) observer.disconnect();
  }

  function resumeObserver() {
    if (!observer || !document.body) return;
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['title', 'aria-label', 'placeholder', 'alt'],
    });
  }

  function apply(root) {
    pauseObserver();
    applying = true;
    try {
      walk(root || document.body || document.documentElement);
      updateControl();
    } finally {
      applying = false;
      resumeObserver();
    }
  }

  function setLang(lang) {
    currentLang = lang === EN ? EN : RU;
    saveLang(currentLang);
    const btn = document.getElementById('lang-toggle');
    if (btn) btn.disabled = true;
    requestAnimationFrame(function () {
      try {
        apply(document.body || document.documentElement);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  }

  function bind() {
    currentLang = storedLang();
    const btn = document.getElementById('lang-toggle');
    if (btn && btn.dataset.i18nBound !== '1') {
      btn.dataset.i18nBound = '1';
      btn.addEventListener('click', () => setLang(currentLang === EN ? RU : EN));
    }
    apply(document.body || document.documentElement);
    if (observer) observer.disconnect();
    observer = new MutationObserver((items) => {
      if (applying) return;
      applying = true;
      try {
        for (const item of items) {
          if (item.type === 'childList') {
            if (currentLang === EN) item.addedNodes.forEach(walk);
          } else if (item.type === 'characterData') {
            if (!textOriginal.has(item.target)) {
              textOriginal.set(item.target, item.target.nodeValue);
            }
            if (currentLang === EN) translateTextNode(item.target);
          } else if (item.type === 'attributes') {
            if (currentLang === EN) translateAttr(item.target, item.attributeName);
          }
        }
      } finally {
        applying = false;
      }
    });
    resumeObserver();
  }

  window.sa02mI18n = {
    get lang() { return currentLang; },
    setLang,
    apply,
    applyDataI18n,
    t: function (s) { return currentLang === EN ? translateText(s) : String(s == null ? '' : s); },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
