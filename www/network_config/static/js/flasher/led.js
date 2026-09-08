/* LED strip (RGBW_WS2812, type 120) settings window — the flasher tab's «led»
   kind, 1.0.6.40 (plan led-window-1.0.6.40, step L4 of led-1.0.6.33).

   Ports the five desktop pages (MR-02m-flasher `feat/led-spy-and-picker`,
   qt_app/module_config/specialized_pages_rgbw.py) into the web config modal:
   «RGBW каналы» · «Входы DI» · «Адресная лента» · «Сцена» (+ the marquee, colour,
   clock, weather and line-indicator cards). EXFX upload is descoped (no tab).

   What lives here and what does not:
     * NO register map, no choice list, no bit mask. Every enum, group range and
       per-effect reg-455 layout arrives in the snapshot (`led.choices`,
       `scene.fx_groups`, `scene.aux_spec`, `scene.visibility`) from the shared
       map through the daemon; this file maps an i18n KEY to its Russian label
       (LED_T) and the i18n observer takes it to English (i18n.js DICT).
     * Every register write is `POST /device_config/led_write` with an action
       from the daemon's allow-list (docs/contracts/led-mb2ws.md §3) — the
       browser never orders or brackets a write.
     * Hide, don't grey: a control the firmware ignores for the selected effect
       is not rendered (scene.visibility, the map's one home); Scale (460) is
       never rendered at all.
     * Write model (plan §9 F3, hybrid): selects, checkboxes, the effect and the
       sliders write on change; the strip batch, the marquee text, the weather
       and the spy cards go by their «Применить» button (each is one daemon
       action that rewrites its whole block).
     * Live values are patched IN PLACE (patch): a focused or edited field
       (dataset.ledDirty) is never overwritten by the 1 s poll — the Carel
       window's rule.

   The file is a classic script: flasher.js is a classic IIFE and cannot
   `import`, and a dynamic import() would add first-open latency for nothing
   (docs/decisions/es-modules.md). It exposes ONE global,
   `window.sa02mLedWindow.create(host)`; the host object is flasher.js's
   injection of the modal plumbing (escapeHtml, t, toast, configModalEl,
   configApi, getState, applyConfigSnapshot, setConfigBusy, setConfigBanner,
   clampInt, currentConfigDevice, currentPort, invalidatePolls, activeElement,
   refreshSnapshot) — no window.* reach-in.

   Verified by scripts/dev/test-led-window.mjs (quality row js-unit-led-window). */
(function () {
  'use strict';

  /** Tab ids of the LED-specific tabs = led_poll.LED_TABS, in order (parity
      pinned by the unit test). The two house tabs (info, network) precede them. */
  const LED_TABS = ['led_pwm', 'led_di', 'led_strip', 'led_scene'];
  /** Actions the window sends = device_config.LED_ACTIONS (parity pinned). */
  const LED_ACTIONS = [
    'pwm', 'di', 'strip', 'scene', 'text', 'clock', 'weather', 'spy',
    'play', 'stop', 'refresh', 'load_flash',
  ];
  /** Keys of the expensive blocks a `panel` poll (base block only) does not
      carry: the previous ones are kept so the cards do not blink empty. */
  const LED_CARRY_KEYS = ['choices', 'pwm', 'di', 'mode_data', 'text', 'text_lines', 'text_lines_raw', 'spy'];
  const TEXT_COLOR_KEYS = ['color1', 'color2', 'bg1', 'bg2'];
  const TEXT_COLOR_IDS = ['cfg-led-tc1', 'cfg-led-tc2', 'cfg-led-bg1', 'cfg-led-bg2'];
  const TEXT_COLOR_LABEL_KEYS = ['rgbw_text_color1', 'rgbw_text_color2', 'rgbw_text_bg1', 'rgbw_text_bg2'];
  const WIRING_KEYS = ['progressive', 'origin_bottom', 'mirror_x', 'swap_xy'];
  const WIRING_IDS = ['cfg-led-mx-prog', 'cfg-led-mx-bottom', 'cfg-led-mx-mirror', 'cfg-led-mx-swap'];
  const WIRING_LABEL_KEYS = [
    'rgbw_strip_matrix_progressive', 'rgbw_strip_matrix_bottom',
    'rgbw_strip_matrix_mirror_x', 'rgbw_strip_matrix_swap_xy',
  ];
  const WX_FIELD_IDS = ['cfg-led-wx-day', 'cfg-led-wx-month', 'cfg-led-wx-temp', 'cfg-led-wx-hum', 'cfg-led-wx-press', 'cfg-led-wx-year'];
  const SPY_SLOT_COUNT = 4;

  /** i18n key → RUSSIAN label — the strings the daemon's keys resolve to
      (ported from the desktop's _i18n_rgbw.py RU column; the EN column is the
      DICT block in i18n.js). Part numbers (WS2812, GRB, 1200) are not keys. */
  const LED_T = {
    'sidebar_rgbw_channels': 'RGBW каналы',
    'rgbw_write_ok': 'Запись выполнена',
    'rgbw_write_pwm_ok': 'Параметры PWM записаны',
    'rgbw_write_di_ok': 'Параметры DI записаны',
    'rgbw_write_strip_ok': 'Параметры адресной ленты записаны',
    'rgbw_write_scene_ok': 'Параметры сцены записаны',
    'sidebar_rgbw_strip': 'Адресная лента',
    'sidebar_rgbw_scene': 'Сцена',
    'sidebar_rgbw_di': 'Входы DI',
    'rgbw_line_single': '1 канал',
    'rgbw_line_dual': '2 канала',
    'rgbw_line_apa102': 'APA102 (SPI, 1 канал)',
    'rgbw_scene_pool': 'Пул пикселей',
    'rgbw_scene_fx': 'Встроенный FX',
    'rgbw_scene_flash': 'Эффект Flash',
    'rgbw_pwm_title': 'Силовые RGBW (PWM)',
    'rgbw_pwm_ch_r': 'Канал R',
    'rgbw_pwm_ch_g': 'Канал G',
    'rgbw_pwm_ch_b': 'Канал B',
    'rgbw_pwm_ch_w': 'Канал W',
    'rgbw_pwm_ch_w1': 'Канал W1',
    'rgbw_pwm_ch_w2': 'Канал W2',
    'rgbw_pwm_ch_w3': 'Канал W3',
    'rgbw_pwm_ch_w4': 'Канал W4',
    'rgbw_pwm_ch_ww': 'WW (тёплый)',
    'rgbw_pwm_ch_cw': 'CW (холодный)',
    'rgbw_pwm_ch_ww1': 'CCT1 WW',
    'rgbw_pwm_ch_cw1': 'CCT1 CW',
    'rgbw_pwm_ch_ww2': 'CCT2 WW',
    'rgbw_pwm_ch_cw2': 'CCT2 CW',
    'rgbw_pwm_ch_4w': '4×W (уровень)',
    'rgbw_pwm_ch_4w_par': '4×W (параллель)',
    'rgbw_pwm_ch_2w_a': '2×W (A)',
    'rgbw_pwm_ch_2w_a_par': '2×W (A∥)',
    'rgbw_pwm_ch_2w_b': '2×W (B)',
    'rgbw_pwm_ch_2w_b_par': '2×W (B∥)',
    'rgbw_pwm_enable': 'Вкл',
    'rgbw_pwm_level': 'Яркость',
    'rgbw_pwm_mode': 'Режим',
    'rgbw_pwm_color_wheel': 'Цветовой круг',
    'rgbw_pwm_mode_rgbw': 'RGB + W',
    'rgbw_pwm_mode_wwww': 'W + W + W + W',
    'rgbw_pwm_mode_cct_cct': 'CCT + CCT',
    'rgbw_pwm_mode_cct_ww': 'CCT + W + W',
    'rgbw_pwm_mode_2w_2w': '2×W + 2×W',
    'rgbw_pwm_mode_4w': '4×W',
    'rgbw_pwm_mode_2w_ww': '2×W + W + W',
    'rgbw_pwm_mode_2w_cct': '2×W + CCT',
    'rgbw_pwm_mode_cct_2w': 'CCT + 2×W',
    'rgbw_pwm_mode_ww_cct': 'W + W + CCT',
    'rgbw_pwm_mode_ww_2w': 'W + W + 2×W',
    'rgbw_pwm_mode_2cct': '2×CCT',
    'rgbw_strip_led_type': 'Тип светодиода',
    'rgbw_strip_byte_order': 'Порядок цветов',
    'rgbw_strip_auto_refresh': 'Автообновление',
    'rgbw_strip_count0': 'Светодиодов на ленте',
    'rgbw_strip_count1': 'Светодиодов на ленте 2',
    'rgbw_strip_format': 'Формат пикселя',
    'rgbw_strip_format_rgb': 'RGB',
    'rgbw_strip_format_rgbw': 'RGBW',
    'rgbw_di_debounce': 'Антидребезг, мс',
    'rgbw_err_value': 'Некорректное значение',
    'rgbw_scene_source': 'Источник сцены',
    'rgbw_scene_fx_id': 'Эффект',
    'rgbw_scene_fx_speed': 'Скорость эффекта',
    'rgbw_scene_fx_param': 'Яркость FX',
    'rgbw_fx_00': 'Статичный',
    'rgbw_fx_01': 'Мигание',
    'rgbw_fx_02': 'Дыхание',
    'rgbw_fx_03': 'Радуга',
    'rgbw_fx_04': 'Радуга по ленте',
    'rgbw_fx_05': 'Радуга-2D',
    'rgbw_fx_06': 'Игра цветов',
    'rgbw_fx_07': 'Праздник',
    'rgbw_fx_08': 'Аврора',
    'rgbw_fx_09': 'Цветовые волны',
    'rgbw_fx_10': 'Спиннер',
    'rgbw_fx_11': 'Конфетти',
    'rgbw_fx_12': 'Матрица: плазма',
    'rgbw_fx_13': 'Матрица: дождь',
    'rgbw_fx_14': 'Ливень',
    'rgbw_fx_15': 'Капля',
    'rgbw_fx_16': 'Дождь по воде',
    'rgbw_fx_17': 'Мыльные пузыри',
    'rgbw_fx_18': 'Круги-всплески',
    'rgbw_fx_19': 'Океан',
    'rgbw_fx_20': 'Морская волна',
    'rgbw_fx_21': 'Прибой',
    'rgbw_fx_22': 'Цунами',
    'rgbw_fx_23': 'Водопад',
    'rgbw_fx_24': 'Брызги',
    'rgbw_fx_25': 'Шторм',
    'rgbw_fx_26': 'Огонь',
    'rgbw_fx_27': 'Камин',
    'rgbw_fx_28': 'Свеча',
    'rgbw_fx_29': 'Лава',
    'rgbw_fx_30': 'Рассвет',
    'rgbw_fx_31': 'Закат',
    'rgbw_fx_32': 'Циркадный',
    'rgbw_fx_33': 'Фейерверк',
    'rgbw_fx_34': 'Салют',
    'rgbw_fx_35': 'Звёздная ночь',
    'rgbw_fx_36': 'Метеоритный дождь',
    'rgbw_fx_37': 'Спираль',
    'rgbw_fx_38': 'Warp',
    'rgbw_fx_39': 'Прыгающие мячи',
    'rgbw_fx_40': 'KITT',
    'rgbw_fx_41': 'Сердцебиение',
    'rgbw_fx_42': 'Песочные часы',
    'rgbw_fx_43': 'Эквалайзер',
    'rgbw_fx_44': 'Узоры',
    'rgbw_fx_45': 'Комета',
    'rgbw_fx_46': 'Сканер',
    'rgbw_fx_47': 'Pac-Man',
    'rgbw_fx_48': 'Заливка цветом',
    'rgbw_fx_49': 'Заливка к центру',
    'rgbw_fx_50': 'Заполнение бегом',
    'rgbw_fx_51': 'Сопровождение',
    'rgbw_fx_52': 'Театральный',
    'rgbw_fx_53': 'Змейка (игра)',
    'rgbw_fx_54': 'Искры (заполнение)',
    'rgbw_fx_55': 'Снегопад',
    'rgbw_fx_56': 'Снежинки (калейдоскоп)',
    'rgbw_fx_57': 'Молния',
    'rgbw_fx_58': 'Летний лес',
    'rgbw_fx_59': 'Осенний лес',
    'rgbw_fx_60': 'Падающая звезда',
    'rgbw_fx_61': 'Ураган',
    'rgbw_fx_62': 'Матрица: текст',
    'rgbw_fx_63': 'Часы HH:MM',
    'rgbw_fx_64': 'Метеостанция',
    'rgbw_fx_65': 'Логотип ЦИНТРОН',
    'rgbw_fx_66': 'Стрелка',
    'rgbw_fx_67': 'Шеврон',
    'rgbw_fx_68': 'Знак «Внимание»',
    'rgbw_fx_69': 'Знак STOP',
    'rgbw_fx_70': 'Авария',
    'rgbw_fx_71': '«ВЫХОД»',
    'rgbw_fx_72': '«ПОЖАР»',
    'rgbw_fx_73': '«ТРЕВОГА»',
    'rgbw_fx_74': '«!»',
    'rgbw_fx_75': 'Уровень',
    'rgbw_fx_76': 'Флаг РФ',
    'rgbw_fx_77': '«РОССИЯ»',
    'rgbw_fx_78': 'Снаряд',
    'rgbw_fx_79': 'Стрела Амура',
    'rgbw_fx_80': 'Метеориты',
    'rgbw_fx_81': 'Индикатор линии',
    'rgbw_fx_grp_base': 'База и заливка',
    'rgbw_fx_grp_color': 'Цветные и радуга',
    'rgbw_fx_grp_water': 'Вода',
    'rgbw_fx_grp_fire': 'Огонь и жар',
    'rgbw_fx_grp_sky': 'Небо и свет',
    'rgbw_fx_grp_phys': 'Физика и геометрия',
    'rgbw_fx_grp_nature': 'Природа и погода',
    'rgbw_fx_grp_text': 'Текст и цифры',
    'rgbw_fx_grp_sign': 'Указатели и таблички',
    'rgbw_fx_grp_special': 'Специальные',
    'rgbw_scene_fx_aux_variant': 'Вариант/направление',
    'rgbw_scene_fx_aux_color': 'Цвет эффекта',
    'rgbw_fx_aux_color_default': 'По умолчанию',
    'rgbw_fx_aux_color_red': 'Красный',
    'rgbw_fx_aux_color_green': 'Зелёный',
    'rgbw_fx_aux_color_blue': 'Синий',
    'rgbw_fx_aux_color_white': 'Белый',
    'rgbw_fx_aux_color_yellow': 'Жёлтый',
    'rgbw_fx_aux_color_orange': 'Оранжевый',
    'rgbw_fx_aux_color_warm_white': 'Тёплый белый',
    'rgbw_scene_fx_aux_style': 'Стиль анимации текста',
    'rgbw_scene_fx_aux_direction': 'Направление',
    'rgbw_scene_fx_aux_percent': 'Процент',
    'rgbw_scene_fx_aux_orientation': 'Другая ориентация',
    'rgbw_scene_fx_aux_escort_color': 'Цвет пятна',
    'rgbw_scene_fx_aux_reverse': 'Обратное направление',
    'rgbw_scene_fx_aux_pool_len': 'Длина пятна, px (0 = 60)',
    'rgbw_scene_fx_aux_stop_stripes': 'Остановить полосы',
    'rgbw_fx_aux_style_hint_two_line': 'В двухстрочном режиме (Строк = 2) работает только стиль «Титры»',
    'rgbw_fx_aux_style_00': 'Авто (статично или бегущая строка)',
    'rgbw_fx_aux_style_01': 'Печатная машинка (мигающий курсор)',
    'rgbw_fx_aux_style_02': 'Проявление по буквам (fade-in)',
    'rgbw_fx_aux_style_03': 'Влёт букв справа по одной',
    'rgbw_fx_aux_style_04': 'Волна (буквы качаются ±1 px)',
    'rgbw_fx_aux_style_05': 'Мигание 1 Гц',
    'rgbw_fx_aux_style_06': 'Всплытие букв снизу',
    'rgbw_fx_aux_style_07': 'Дешифровка (глифы оседают в текст)',
    'rgbw_fx_aux_style_08': 'Глитч (сдвиги-двойники)',
    'rgbw_fx_aux_style_09': 'Неон (случайная буква притухает)',
    'rgbw_fx_aux_style_10': 'Падение сверху с отскоками',
    'rgbw_fx_aux_style_11': 'Растворение по пикселям',
    'rgbw_fx_aux_style_12': 'Радуга (подменяет цвет)',
    'rgbw_fx_aux_style_13': 'Проезд насквозь (справа → пауза → влево)',
    'rgbw_fx_aux_style_14': 'Разлёт на частицы и кольца',
    'rgbw_fx_aux_style_15': 'Морфинг «A|B» (перетекание слова A в B)',
    'rgbw_fx_aux_style_16': 'Титры (вертикальная прокрутка)',
    'rgbw_fx_aux_style_17': 'Дрожь (джиттер ±1 px)',
    'rgbw_fx_aux_style_hint_morph': 'Стиль «Морфинг» требует разделитель | в тексте (без него — растворение)',
    'rgbw_fx_aux_style_hint_wide': 'Текст шире полотна всегда идёт обычной бегущей строкой, независимо от стиля',
    'rgbw_fx_aux_dir_right': 'Вправо',
    'rgbw_fx_aux_dir_left': 'Влево',
    'rgbw_fx_aux_dir_up': 'Вверх',
    'rgbw_fx_aux_dir_down': 'Вниз',
    'rgbw_scene_fx_density': 'Плотность',
    'rgbw_strip_gamma': 'Гамма',
    'rgbw_strip_matrix_progressive': 'Прогрессивная развёртка',
    'rgbw_strip_matrix_bottom': 'Начало снизу',
    'rgbw_mx_type': 'Тип матрицы',
    'rgbw_mx_8x8': '8×8 (ш8×в8)',
    'rgbw_mx_8x16': '8×16 (ш8×в16)',
    'rgbw_mx_16x8': '16×8 (ш16×в8)',
    'rgbw_mx_16x16': '16×16 (ш16×в16)',
    'rgbw_mx_8x32': '8×32 (ш8×в32)',
    'rgbw_mx_32x8': '32×8 (ш32×в8)',
    'rgbw_mx_16x32': '16×32 (ш16×в32)',
    'rgbw_mx_32x16': '32×16 (ш32×в16)',
    'rgbw_mx_32x32': '32×32 (ш32×в32)',
    'rgbw_mx_64x16': '64×16 (ш64×в16)',
    'rgbw_strip_matrix_mirror_x': 'Зеркально по X',
    'rgbw_strip_matrix_swap_xy': 'Столбцами (swap XY)',
    'rgbw_strip_tile_count': 'Число матриц',
    'rgbw_tile_count_auto': 'Авто',
    'rgbw_tile_count_1': '1',
    'rgbw_tile_count_2': '2',
    'rgbw_tile_count_3': '3',
    'rgbw_tile_count_4': '4',
    'rgbw_strip_tile_mode': 'Эффект на матрицах',
    'rgbw_tile_mode_span': 'Растянуть на всё полотно',
    'rgbw_tile_mode_replicate': 'Повторить на каждой',
    'rgbw_tile_mode_mirror': 'Зеркально (калейдоскоп)',
    'rgbw_ch2_title': 'Канал 2',
    'rgbw_ch2_note': 'Общее для обоих каналов и раздельно НЕ настраивается: тип светодиода, формат пикселя, порядок цветов, гамма, эффект, скорость и яркость. Например, WS2812 на канале 1 и SK6812 на канале 2 одновременно невозможны. Своё у канала 2 только два параметра — длина и режим. Буфер пикселей тоже общий: (длина 1 + длина 2) × 3 байта (RGB) или × 4 (RGBW) не больше 3072.',
    'rgbw_ch2_mode_note': 'Что показывает канал 2: «Синхронно» и «Зеркально» — ту же картинку, что канал 1 (зеркально — в обратном порядке); длина канала 2 в этих режимах не используется. «Продолжение полотна» — одно общее полотно, канал 2 продолжает канал 1. «Независимо» — свой участок буфера, но эффекты рисуют только канал 1: эти пиксели пишет мастер по Modbus (пул с адреса 1100 + 2 × длина канала 1), сам модуль их не формирует. Нужны эффекты на обоих выходах — «Синхронно», «Зеркально» или «Продолжение полотна».',
    'rgbw_strip_ch2_mode': 'Режим канала 2',
    'rgbw_ch2_off': 'Выкл',
    'rgbw_ch2_sync': 'Синхронно (та же картинка)',
    'rgbw_ch2_independent': 'Независимо (пиксели от мастера)',
    'rgbw_ch2_mirror': 'Зеркально (та же картинка наоборот)',
    'rgbw_ch2_continuation': 'Продолжение полотна',
    'rgbw_text_window': 'Бегущая строка',
    'rgbw_card_clock': 'Часы',
    'rgbw_card_weather': 'Метеостанция',
    'rgbw_card_spy': 'Индикатор линии',
    'rgbw_spy_work_port': 'Рабочий порт',
    'rgbw_spy_port_off': 'Выкл (оба слейва)',
    'rgbw_spy_port_a': 'Порт A (торцевая)',
    'rgbw_spy_port_b': 'Порт B (внешний)',
    'rgbw_spy_work_mode': 'Режим порта',
    'rgbw_spy_mode_off': 'Выкл',
    'rgbw_spy_mode_spy': 'Spy (подслушка)',
    'rgbw_spy_mode_master': 'Master (опрос)',
    'rgbw_spy_tap': 'Подслушка на слейв-порту',
    'rgbw_spy_baud': 'Скорость WorkPort',
    'rgbw_spy_parity': 'Чётность',
    'rgbw_spy_parity_none': 'Без чётности',
    'rgbw_spy_parity_even': 'Чёт',
    'rgbw_spy_parity_odd': 'Нечет',
    'rgbw_spy_stop': 'Стоп-биты',
    'rgbw_spy_poll_ms': 'Период опроса, мс',
    'rgbw_spy_timeout_ms': 'Таймаут ответа, мс',
    'rgbw_spy_stale_ms': 'Устаревание, мс',
    'rgbw_spy_status': 'Статус',
    'rgbw_spy_live': 'Значения',
    'rgbw_spy_refresh': 'Обновить',
    'rgbw_spy_params': 'Параметры прослушки',
    'rgbw_spy_addresses': 'Адреса прослушки',
    'rgbw_spy_slot': 'Слот',
    'rgbw_spy_uid': 'Адрес',
    'rgbw_spy_fc': 'FC',
    'rgbw_spy_reg': 'Регистр',
    'rgbw_spy_type': 'Тип',
    'rgbw_spy_decimals': 'Знаков после запятой',
    'rgbw_spy_unit': 'Размерность',
    'rgbw_spy_lo': 'Низ',
    'rgbw_spy_hi': 'Верх',
    'rgbw_spy_lim_hint': 'lo=hi (в т.ч. 0/0) — выкл. Ниже — синий, выше — красный, в диапазоне — цвет 463/464',
    'rgbw_spy_fc_03': '03 Holding',
    'rgbw_spy_fc_04': '04 Input',
    'rgbw_spy_fc_06': '06 Write single',
    'rgbw_spy_fc_16': '16 Write multi',
    'rgbw_spy_type_int16': 'INT16',
    'rgbw_spy_type_uint16': 'UINT16',
    'rgbw_spy_type_int32_ab': 'INT32 AB',
    'rgbw_spy_type_uint32_ab': 'UINT32 AB',
    'rgbw_spy_type_int32_cdab': 'INT32 CDAB',
    'rgbw_spy_type_uint32_cdab': 'UINT32 CDAB',
    'rgbw_spy_type_float_ab': 'FLOAT ABCD (BE)',
    'rgbw_spy_type_float_cdab': 'FLOAT CDAB',
    'rgbw_spy_type_float_badc': 'FLOAT BADC',
    'rgbw_spy_type_float_dcba': 'FLOAT DCBA (LE)',
    'rgbw_spy_type_time_hh': 'Время ЧЧ',
    'rgbw_spy_type_time_mm': 'Время ММ',
    'rgbw_spy_type_time_ss': 'Время СС',
    'rgbw_spy_time_hint': 'Тип «Время ЧЧ/ММ/СС» (10/11/12) собирает одни часы: ЧЧ+ММ+СС → 14:05:09, без секунд → 14:05, без часов → 05:09. Адрес 0 — поле не задано и на ленту не попадает. Двоеточие мигает как в режиме 64. Остальные слоты — обычные значения. 2 строки: часы сверху, значения снизу.',
    'rgbw_wx_spy': 'Прослушка полей',
    'rgbw_wx_spy_hint': 'UID 0 — поле не показывается. Если задан хотя бы один UID, лента берёт только прослушку (локальные 453/457–462 игнорируются). Нужен WorkPort (карта «Индикатор линии»). Не сохраняется при выключении.',
    'rgbw_wx_spy_hh': 'ЧЧ',
    'rgbw_wx_spy_mm': 'ММ',
    'rgbw_wx_spy_ss': 'СС',
    'rgbw_wx_spy_date': 'Дата',
    'rgbw_wx_spy_temp': 'Температура',
    'rgbw_wx_spy_hum': 'Влажность',
    'rgbw_wx_spy_press': 'Давление',
    'rgbw_card_colors': 'Цвета текста',
    'rgbw_text_hint_upper': 'Шрифт 5×7 — только ЗАГЛАВНЫЕ; текст приводится к верхнему регистру',
    'rgbw_text_lines': 'Размер текста',
    'rgbw_text_lines_single': 'Одна строка, крупный шрифт 2x (10×14)',
    'rgbw_text_lines_double': 'Две строки, мелкий 5×7 (разделитель |)',
    'rgbw_text_lines_hint_2x': 'Крупный 2x — только пока текст целиком влезает в ширину полотна (%d px на символ, на %d px помещается %d символов); иначе прошивка сама переходит на 5×7 и прокручивает.',
    'rgbw_text_color1': 'Цвет текста 1',
    'rgbw_text_color2': 'Цвет текста 2',
    'rgbw_text_bg1': 'Фон текста 1',
    'rgbw_text_bg2': 'Фон текста 2',
    'rgbw_clock_hh': 'ЧЧ',
    'rgbw_clock_mm': 'ММ',
    'rgbw_wx_date': 'Дата',
    'rgbw_wx_temp': 'Температура',
    'rgbw_wx_hum': 'Влажность',
    'rgbw_wx_press': 'Давление',
    'rgbw_wx_year': 'Год',
    'rgbw_wx_temp_color': 'Цвет температуры',
    'rgbw_wx_lines': 'Вывод значений',
    'rgbw_wx_lines_single': '1 строка',
    'rgbw_wx_lines_double': '2 строки',
    'rgbw_clock_sync_pc': 'Время с ПК',
    'rgbw_clock_unset': 'Сбросить',
    'rgbw_scene_flash_slot': 'Слот Flash',
    'rgbw_scene_loop': 'Цикл',
    'rgbw_scene_play': 'Пуск',
    'rgbw_scene_stop': 'Стоп',
    'rgbw_scene_load_flash': 'Загрузить Flash',
    'rgbw_scene_refresh': 'Обновить',
    'rgbw_di_mode': 'Режим входа',
    'rgbw_di_mode_btn': 'Кнопка',
    'rgbw_di_mode_sw': 'Выключатель',
    'rgbw_current_ma': 'Ток, мА',
    'rgbw_ntc': 'NTC платы, °C',
    'rgbw_vled': 'VLED, В',
  };

  function T(key) {
    return Object.prototype.hasOwnProperty.call(LED_T, key) ? LED_T[key] : String(key == null ? '' : key);
  }

  function pad2(n) { return String(n).padStart(2, '0'); }

  function fxKey(id) { return 'rgbw_fx_' + pad2(id); }

  /** `%d` placeholders of the desktop hint filled with the daemon's metrics. */
  function fmtHint(key, args) {
    let i = 0;
    return T(key).replace(/%d/g, () => String((args || [])[i++]));
  }

  function create(host) {
    const esc = host.escapeHtml;
    const el = host.configModalEl;

    function led(snap) { return (snap && snap.led) || {}; }
    function choicesOf(snap) { return led(snap).choices || null; }
    function currentSnap() { return host.getState().configSnapshot; }

    /* ── markup helpers (every dynamic value passes through esc) ─────────── */

    function optionsHtml(pairs, current, labelOf) {
      const cur = current == null ? '' : String(current);
      return (pairs || []).map(function (pair) {
        const code = pair[0];
        const label = labelOf ? labelOf(pair[1]) : T(pair[1]);
        return `<option value="${esc(code)}"${String(code) === cur ? ' selected' : ''}>${esc(label)}</option>`;
      }).join('');
    }

    function selectHtml(id, pairs, current, labelOf) {
      return `<select id="${id}" data-led-field="1">${optionsHtml(pairs, current, labelOf)}</select>`;
    }

    function numberHtml(id, value, min, max, step) {
      const v = value == null ? '' : String(value);
      return `<input id="${id}" data-led-field="1" type="number" min="${esc(min)}" max="${esc(max)}" step="${esc(step == null ? 1 : step)}" value="${esc(v)}" />`;
    }

    // Own class, not .checkbox-line: the form's `input { width: 100% }` and the
    // global checkbox-line rules (grid-column, width) turn a checkbox into a
    // card-wide box that pushes its label off the card (seen in the first render
    // pass); .cfg-led-check pins the box to its intrinsic size.
    function checkHtml(id, checked, label) {
      return `<label class="cfg-led-check"><input id="${id}" data-led-field="1" type="checkbox"${checked ? ' checked' : ''} /> ${esc(label)}</label>`;
    }

    function rangeHtml(id, value, min, max) {
      const v = value == null ? '' : String(value);
      return `<div class="cfg-led-range"><input id="${id}" data-led-field="1" type="range" min="${esc(min)}" max="${esc(max)}" value="${esc(v)}" /><output id="${id}-val">${esc(v)}</output></div>`;
    }

    function colorHtml(id, hex) {
      return `<input id="${id}" data-led-field="1" type="color" value="${esc(hex || '#000000')}" />`;
    }

    /** One label + field pair of a .flasher-config-form grid. `show` names the
        strip-tab toggle group ("ws" / "dual"); a hidden pair stays in the DOM
        so the line selector can reveal it without a re-render. */
    function pair(id, label, fieldHtml, show, hidden, unit) {
      const attrs = (show ? ` data-led-show="${esc(show)}"` : '') + (hidden ? ' hidden' : '');
      // A unit / index rides in its own span: the i18n observer translates per
      // text node, so «Дата» stays a DICT key and «(dd)» stays untranslated.
      const unitHtml = unit ? ` <span class="cfg-led-unit">${esc(unit)}</span>` : '';
      return `<div class="cfg-led-pair"${attrs}><label for="${id}">${esc(label)}${unitHtml}</label>${fieldHtml}</div>`;
    }

    function fullRow(inner) { return `<div class="cfg-led-pair cfg-led-pair--full">${inner}</div>`; }

    function note(text) { return `<div class="flasher-config-note">${esc(text)}</div>`; }

    function empty(text) { return `<div class="flasher-empty">${esc(text)}</div>`; }

    function card(title, body, extraAttrs) {
      return `<section class="flasher-config-card"${extraAttrs || ''}>${title ? `<h4>${esc(title)}</h4>` : ''}${body}</section>`;
    }

    function grid(inner) { return `<div class="flasher-config-grid">${inner}</div>`; }

    /** A command button. `idAttr` is the LITERAL  attribute so the
        html-id-contract gate sees the id this file later looks up by selector. */
    function cmdBtn(idAttr, label, cls) {
      return `<button class="btn ${cls || 'btn-sm'}" type="button" ${idAttr} data-led-cmd="1">${esc(label)}</button>`;
    }

    function readout(value, digits) {
      if (value == null || value === '') return '—';
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(digits) : '—';
    }

    /* ── gates: hide, don't grey ────────────────────────────────────────── */

    /** Markup that replaces a tab when it cannot honestly render — no inputs
        at all, never controls holding values the device did not give. */
    function unavailable(snap) {
      const L = led(snap);
      if (L.answered === false) {
        return grid(card('', empty('Нет данных') + note('Лента не отвечает — проверьте линию и адрес')));
      }
      if (!choicesOf(snap)) {
        return grid(card('', empty('Нет данных') + note('Демон прошивальщика старее веб-интерфейса (нет списков выбора) — обновите пакет sa02m-flasher')));
      }
      return '';
    }

    /* ── tabs, titles, poll shape ───────────────────────────────────────── */

    function tabs(snap) {
      const sc = led(snap).scene;
      const playing = !!(sc && Number(sc.play_ctrl) === 1);
      const suffix = sc ? ' - ' + (playing ? T('rgbw_scene_play') : T('rgbw_scene_stop')) : '';
      return [
        { id: 'info', label: 'Сведения' },
        { id: 'network', label: 'Сеть' },
        { id: LED_TABS[0], label: T('sidebar_rgbw_channels') },
        { id: LED_TABS[1], label: T('sidebar_rgbw_di') },
        { id: LED_TABS[2], label: T('sidebar_rgbw_strip') },
        { id: LED_TABS[3], label: T('sidebar_rgbw_scene'), suffix: suffix, live: playing },
      ];
    }

    function title() { return 'Светодиодная лента LED'; }
    function kicker() { return 'Настройка светодиодной ленты'; }

    /** The `active_tab` a poll sends. The 1 s `panel` tick of the scene tab goes
        with an empty tab: the base block only (one transaction), never the
        453/494/516/640 set — those are read on tab entry, after every write
        and on the spy «Обновить» (plan §8, F6). */
    function pollTab(tab, detail) {
      const t = String(tab || '');
      if (t === LED_TABS[3] && detail === 'panel') return '';
      return t;
    }

    /* ── «RGBW каналы» ──────────────────────────────────────────────────── */

    function renderPwmTab(snap) {
      const gate = unavailable(snap);
      if (gate) return gate;
      const p = led(snap).pwm || {};
      const ch = choicesOf(snap);
      const mode = p.mode == null ? null : Number(p.mode);
      let rows = '';
      if (mode == null) {
        rows = empty('Нет данных');
      } else {
        // Literal ids on the controls wire* looks up by selector (the gate's grammar).
        rows += pair('cfg-led-pwm-mode', T('rgbw_pwm_mode'), `<select id="cfg-led-pwm-mode" data-led-field="1">${optionsHtml(ch.pwm_modes, mode)}</select>`);
        if (mode === 0) {
          // The colour wheel: RGB + W mode only (the other modes drive white
          // channels, where a colour has no meaning).
          rows += pair('cfg-led-pwm-color', T('rgbw_pwm_color_wheel'), `<input id="cfg-led-pwm-color" data-led-field="1" type="color" value="${esc(p.color_hex || '#000000')}" />`);
        }
        const labels = p.mode_labels || [];
        const visible = p.mode_visible || [];
        const levels = p.levels || [];
        for (let i = 0; i < 4; i++) {
          // A parallel slave channel is hidden, not greyed (map: mode_visible).
          if (visible[i] === false) continue;
          const n = i + 1;
          const lvl = levels[i];
          rows += `<div class="cfg-led-pair"><label for="cfg-led-pwm-lvl-${n}">${esc(T(labels[i] || ''))}</label>` +
            `<div class="cfg-led-inline">${checkHtml(`cfg-led-pwm-en-${n}`, Number(lvl) > 0, T('rgbw_pwm_enable'))}` +
            `<span class="cfg-led-inline-label">${esc(T('rgbw_pwm_level'))}</span>` +
            `${numberHtml(`cfg-led-pwm-lvl-${n}`, lvl == null ? '' : lvl, 0, ch.pwm_permille_max || 1000)}</div></div>`;
        }
      }
      const tele = `<dl class="flasher-config-kv">` +
        [1, 2, 3, 4].map(n => `<div><dt><span>${esc(T('rgbw_current_ma'))}</span> ${n}</dt><dd id="cfg-led-pwm-cur-${n}">${esc(readout(p.currents_ma && p.currents_ma[n - 1], 0))}</dd></div>`).join('') +
        `<div><dt>${esc(T('rgbw_ntc'))}</dt><dd id="cfg-led-ntc">${esc(readout(p.ntc_c, 1))}</dd></div>` +
        `<div><dt>${esc(T('rgbw_vled'))}</dt><dd id="cfg-led-vled">${esc(readout(p.vled_v, 2))}</dd></div></dl>`;
      return grid(
        card(T('rgbw_pwm_title'), `<div class="flasher-config-form">${rows}</div>`) +
        card('Телеметрия', tele)
      );
    }

    function patchPwm(L, set, text) {
      const p = L.pwm || {};
      set('cfg-led-pwm-mode', p.mode);
      set('cfg-led-pwm-color', p.color_hex);
      const levels = p.levels || [];
      for (let i = 0; i < 4; i++) {
        if (levels[i] == null) continue;
        set(`cfg-led-pwm-en-${i + 1}`, Number(levels[i]) > 0);
        set(`cfg-led-pwm-lvl-${i + 1}`, levels[i]);
      }
      for (let n = 1; n <= 4; n++) text(`cfg-led-pwm-cur-${n}`, readout(p.currents_ma && p.currents_ma[n - 1], 0));
      text('cfg-led-ntc', readout(p.ntc_c, 1));
      text('cfg-led-vled', readout(p.vled_v, 2));
    }

    function wirePwm(body) {
      const mode = body.querySelector('#cfg-led-pwm-mode');
      if (mode) mode.addEventListener('change', () => ledCommand('pwm', { mode: host.clampInt(mode.value, 0, 255, 0) }, mode, T('rgbw_write_pwm_ok')));
      const color = body.querySelector('#cfg-led-pwm-color');
      if (color) color.addEventListener('change', () => ledCommand('pwm', { color: String(color.value || '') }, color, T('rgbw_write_pwm_ok')));
      for (let n = 1; n <= 4; n++) {
        const en = body.querySelector(`#cfg-led-pwm-en-${n}`);
        const lvl = body.querySelector(`#cfg-led-pwm-lvl-${n}`);
        if (!en || !lvl) continue;
        const send = function () {
          // Unchecked ⇒ 0 (the desktop rule): the level box keeps its number
          // so re-enabling restores it.
          const value = en.checked ? host.clampInt(lvl.value, 0, 1000, 0) : 0;
          ledCommand('pwm', { channel: n - 1, value: value }, lvl.parentNode || lvl, T('rgbw_write_pwm_ok'));
        };
        en.addEventListener('change', send);
        lvl.addEventListener('change', send);
      }
    }

    /* ── «Входы DI» ─────────────────────────────────────────────────────── */

    function renderDiTab(snap) {
      const gate = unavailable(snap);
      if (gate) return gate;
      const d = led(snap).di || {};
      const ch = choicesOf(snap);
      const chans = Array.isArray(d.channels) ? d.channels : [];
      if (!chans.length) return grid(card(T('sidebar_rgbw_di'), empty('Нет данных')));
      const rows = chans.map(function (c) {
        const n = Number(c.channel);
        return `<div class="cfg-led-pair"><label for="cfg-led-di-mode-${n}">IN${n}</label>` +
          `<div class="cfg-led-inline">${selectHtml(`cfg-led-di-mode-${n}`, ch.di_modes, c.mode)}` +
          `<span class="cfg-led-inline-label">${esc(T('rgbw_di_debounce'))}</span>` +
          `${numberHtml(`cfg-led-di-deb-${n}`, c.debounce_ms, 10, 2000)}</div></div>`;
      }).join('');
      return grid(card(T('sidebar_rgbw_di'), `<div class="flasher-config-form">${rows}</div>`));
    }

    function patchDi(L, set) {
      const chans = (L.di && L.di.channels) || [];
      chans.forEach(function (c) {
        set(`cfg-led-di-mode-${c.channel}`, c.mode);
        set(`cfg-led-di-deb-${c.channel}`, c.debounce_ms);
      });
    }

    function wireDi(body) {
      for (let n = 1; n <= 4; n++) {
        const mode = body.querySelector(`#cfg-led-di-mode-${n}`);
        if (mode) mode.addEventListener('change', () => ledCommand('di', { channel: n - 1, mode: host.clampInt(mode.value, 0, 1, 0) }, mode, T('rgbw_write_di_ok')));
        const deb = body.querySelector(`#cfg-led-di-deb-${n}`);
        if (deb) deb.addEventListener('change', () => ledCommand('di', { channel: n - 1, debounce_ms: host.clampInt(deb.value, 10, 2000, 50) }, deb, T('rgbw_write_di_ok')));
      }
    }

    /* ── «Адресная лента» ───────────────────────────────────────────────── */

    function matrixTypeOptions(ch, s) {
      const geom = `${Number(s.matrix_width) || 0},${Number(s.matrix_height) || 0}`;
      const listed = (ch.matrix_types || []).some(t => `${t[1]},${t[2]}` === geom);
      // A device may hold a geometry no listed type matches (firmware takes any
      // 1..64 × 8..32): it is SHOWN as read, never silently rewritten (map rule).
      const extra = listed || !s.matrix_geometry
        ? ''
        : `<option value="${esc(geom)}" selected>${esc(s.matrix_geometry)}</option>`;
      return extra + (ch.matrix_types || []).map(function (t) {
        const v = `${t[1]},${t[2]}`;
        return `<option value="${esc(v)}"${v === geom ? ' selected' : ''}>${esc(T(t[0]))}</option>`;
      }).join('');
    }

    function renderStripTab(snap) {
      const gate = unavailable(snap);
      if (gate) return gate;
      const s = led(snap).strip || {};
      const ch = choicesOf(snap);
      const line = s.line_ui || 'single_ws';
      const dual = line === 'dual_ws';
      const apa = line === 'apa102';
      const apaCode = ch.led_type_apa102;
      // APA102 is chosen through the line selector, so the LED-type combo
      // hides that code (the desktop does the same).
      const ledTypes = (ch.led_types || []).filter(t => apaCode == null || String(t[0]) !== String(apaCode));
      const wiring = s.matrix_wiring || {};
      let rows = '';
      // Reg 413 lives on this tab (plan §1): it scopes the channel-2 block and
      // rides the same «Применить» batch as LedType / counts / format.
      rows += pair('cfg-led-line', 'Выходы ленты', `<select id="cfg-led-line" data-led-field="1">${optionsHtml(ch.line_ui, line)}</select>`);
      rows += pair('cfg-led-ledtype', T('rgbw_strip_led_type'), selectHtml('cfg-led-ledtype', ledTypes, s.led_type, k => k), 'ws', apa);
      rows += pair('cfg-led-pixfmt', T('rgbw_strip_format'), selectHtml('cfg-led-pixfmt', ch.pixel_formats, s.pixel_format), 'ws', apa);
      rows += pair('cfg-led-order', T('rgbw_strip_byte_order'), selectHtml('cfg-led-order', ch.byte_orders, s.byte_order, k => k));
      rows += pair('cfg-led-count0', T('rgbw_strip_count0'), numberHtml('cfg-led-count0', s.led_count0, 0, ch.led_count0_max || 1024));
      rows += fullRow(checkHtml('cfg-led-autorefresh', Number(s.auto_refresh) === 1, T('rgbw_strip_auto_refresh')));
      rows += pair('cfg-led-gamma', T('rgbw_strip_gamma'), numberHtml('cfg-led-gamma', s.gamma, 0, 255));
      rows += pair('cfg-led-mxtype', T('rgbw_mx_type'), `<select id="cfg-led-mxtype" data-led-field="1">${matrixTypeOptions(ch, s)}</select>`);
      rows += `<div class="cfg-led-pair cfg-led-pair--full"><span class="cfg-led-group-label">${esc('Развёртка матрицы')}</span><div class="cfg-led-radios">` +
        WIRING_IDS.map((id, i) => checkHtml(id, !!wiring[WIRING_KEYS[i]], T(WIRING_LABEL_KEYS[i]))).join('') + `</div></div>`;
      rows += pair('cfg-led-tilecount', T('rgbw_strip_tile_count'), selectHtml('cfg-led-tilecount', ch.tile_counts, s.matrix_tile_count));
      rows += pair('cfg-led-tilemode', T('rgbw_strip_tile_mode'), selectHtml('cfg-led-tilemode', ch.tile_modes, s.matrix_tile_mode));
      const actions = `<div class="flasher-config-actions">${cmdBtn('id="cfg-led-strip-apply-btn"', 'Применить', 'btn-primary')}</div>`;
      const ch2 = `<div class="flasher-config-form">` +
        pair('cfg-led-count1', T('rgbw_strip_count1'), numberHtml('cfg-led-count1', s.led_count1, 0, ch.led_count1_max || 512)) +
        pair('cfg-led-ch2mode', T('rgbw_strip_ch2_mode'), selectHtml('cfg-led-ch2mode', ch.ch2_modes, s.ch2_mode)) +
        `</div>` + note(T('rgbw_ch2_mode_note')) + note(T('rgbw_ch2_note'));
      return grid(
        card(T('sidebar_rgbw_strip'), `<div class="flasher-config-form">${rows}</div>${actions}`) +
        // The whole channel-2 block is DUAL-only: no second output exists in
        // the other modes, and firmware rejects Ch2Mode ≠ 0 outside DUAL_WS.
        card(T('rgbw_ch2_title'), ch2, ` data-led-show="dual"${dual ? '' : ' hidden'}`)
      );
    }

    function patchStrip(L, set) {
      const s = L.strip || {};
      if (!L.strip) return;
      set('cfg-led-line', s.line_ui);
      set('cfg-led-ledtype', s.led_type);
      set('cfg-led-pixfmt', s.pixel_format);
      set('cfg-led-order', s.byte_order);
      set('cfg-led-count0', s.led_count0);
      set('cfg-led-autorefresh', Number(s.auto_refresh) === 1);
      set('cfg-led-gamma', s.gamma);
      set('cfg-led-mxtype', `${Number(s.matrix_width) || 0},${Number(s.matrix_height) || 0}`);
      const wiring = s.matrix_wiring || {};
      WIRING_IDS.forEach((id, i) => set(id, !!wiring[WIRING_KEYS[i]]));
      set('cfg-led-tilecount', s.matrix_tile_count);
      set('cfg-led-tilemode', s.matrix_tile_mode);
      set('cfg-led-count1', s.led_count1);
      set('cfg-led-ch2mode', s.ch2_mode);
    }

    /** The «Адресная лента» batch — one `strip` action, field names of the
        contract (led-mb2ws.md §3). Rows the line mode hides are omitted: the
        daemon's line-mode builder forces them (APA102) or zeroes them (no
        second output). */
    function ledStripParams() {
      const v = id => { const e = el(id); return e ? e.value : null; };
      const on = id => { const e = el(id); return !!(e && e.checked); };
      const line = String(v('cfg-led-line') || 'single_ws');
      const geom = String(v('cfg-led-mxtype') || '').split(',');
      const params = {
        line: line,
        byte_order: host.clampInt(v('cfg-led-order'), 0, 0xFFFF, 0),
        led_count0: host.clampInt(v('cfg-led-count0'), 0, 0xFFFF, 0),
        auto_refresh: on('cfg-led-autorefresh') ? 1 : 0,
        gamma: host.clampInt(v('cfg-led-gamma'), 0, 255, 0),
        matrix_width: host.clampInt(geom[0], 1, 64, 16),
        matrix_height: host.clampInt(geom[1], 0, 255, 16),
        tile_count: host.clampInt(v('cfg-led-tilecount'), 0, 15, 0),
        tile_mode: host.clampInt(v('cfg-led-tilemode'), 0, 7, 0),
      };
      WIRING_KEYS.forEach((key, i) => { params[key] = on(WIRING_IDS[i]); });
      if (line !== 'apa102') {
        params.led_type = host.clampInt(v('cfg-led-ledtype'), 0, 0xFFFF, 0);
        params.pixel_format = host.clampInt(v('cfg-led-pixfmt'), 0, 1, 0);
      }
      if (line === 'dual_ws') {
        params.led_count1 = host.clampInt(v('cfg-led-count1'), 0, 0xFFFF, 0);
        params.ch2_mode = host.clampInt(v('cfg-led-ch2mode'), 0, 4, 0);
      }
      return params;
    }

    function wireStrip(body) {
      const line = body.querySelector('#cfg-led-line');
      if (line) {
        line.addEventListener('change', function () {
          const apa = line.value === 'apa102';
          const dual = line.value === 'dual_ws';
          body.querySelectorAll('[data-led-show="ws"]').forEach(function (n) { n.hidden = apa; });
          body.querySelectorAll('[data-led-show="dual"]').forEach(function (n) { n.hidden = !dual; });
        });
      }
      const apply = body.querySelector('#cfg-led-strip-apply-btn');
      if (apply) apply.addEventListener('click', () => ledCommand('strip', ledStripParams(), body, T('rgbw_write_strip_ok')));
    }

    /* ── «Сцена» ────────────────────────────────────────────────────────── */

    function playBadge(sc) {
      const ctl = sc ? Number(sc.play_ctrl) : NaN;
      if (ctl === 1) return { cls: 'badge-ok', label: T('rgbw_scene_play') };
      if (ctl === 2) return { cls: 'badge-warn', label: 'Пауза' };
      if (ctl === 0) return { cls: 'badge-unk', label: T('rgbw_scene_stop') };
      return { cls: 'badge-unk', label: 'Нет данных' };
    }

    function auxHintsHtml(keys) {
      return (keys || []).map(k => `<div>${esc(T(k))}</div>`).join('');
    }

    function renderSceneTab(snap) {
      const gate = unavailable(snap);
      if (gate) return gate;
      const L = led(snap);
      const sc = L.scene || {};
      const ch = choicesOf(snap);
      const md = L.mode_data || {};
      const vis = sc.visibility || {};
      const src = sc.scene_ui || 'pool';
      const isFx = src === 'fx';
      const isFlash = src === 'flash';
      const badge = playBadge(sc);
      let rows = '';
      rows += `<div class="cfg-led-pair cfg-led-pair--full"><span class="cfg-led-group-label">${esc(T('rgbw_scene_source'))}</span><div class="cfg-led-radios">` +
        (ch.scene_ui || []).map(p => `<label><input type="radio" name="cfg-led-src" id="cfg-led-src-${esc(p[0])}" value="${esc(p[0])}" data-led-field="1"${String(p[0]) === src ? ' checked' : ''} /> ${esc(T(p[1]))}</label>`).join('') +
        `</div></div>`;
      if (isFx) {
        const groups = sc.fx_groups || [];
        const gid = sc.fx_group || (groups[0] && groups[0].id) || '';
        const group = groups.find(g => g.id === gid) || groups[0] || { first: 0, last: -1 };
        const fxPairs = [];
        for (let id = Number(group.first); id <= Number(group.last); id++) fxPairs.push([id, fxKey(id)]);
        rows += `<div class="cfg-led-pair cfg-led-pair--full"><span class="cfg-led-group-label">${esc('Группа эффектов')}</span><div class="cfg-led-radios">` +
          groups.map(g => `<label><input type="radio" name="cfg-led-fxgrp" id="cfg-led-fxgrp-${esc(g.id)}" value="${esc(g.id)}" data-led-field="1"${g.id === gid ? ' checked' : ''} /> ${esc(T(g.key))}</label>`).join('') +
          `</div></div>`;
        rows += pair('cfg-led-fx', T('rgbw_scene_fx_id'), selectHtml('cfg-led-fx', fxPairs, sc.fx_id));
        rows += pair('cfg-led-speed', T('rgbw_scene_fx_speed'), rangeHtml('cfg-led-speed', sc.fx_speed, 0, 255));
        rows += pair('cfg-led-bri', T('rgbw_scene_fx_param'), rangeHtml('cfg-led-bri', sc.fx_param, 0, 255));
        if (vis.fx_density) rows += pair('cfg-led-density', T('rgbw_scene_fx_density'), rangeHtml('cfg-led-density', md.fx_density, 0, 255));
        rows += renderAuxRows(sc, md, ch, vis);
      }
      if (isFlash) {
        rows += pair('cfg-led-slot', T('rgbw_scene_flash_slot'), `<input id="cfg-led-slot" data-led-field="1" type="number" min="0" max="31" step="1" value="${esc(sc.flash_slot == null ? '' : sc.flash_slot)}" />`);
        rows += fullRow(checkHtml('cfg-led-loop', !!sc.loop, T('rgbw_scene_loop')));
      }
      const head = `<div class="cfg-led-head"><span class="badge ${badge.cls}" id="cfg-led-play-badge">${esc(badge.label)}</span></div>`;
      const actions = `<div class="flasher-config-actions">` +
        cmdBtn('id="cfg-led-play-btn"', T('rgbw_scene_play'), 'btn-primary') +
        cmdBtn('id="cfg-led-stop-btn"', T('rgbw_scene_stop'), 'btn-warn') +
        cmdBtn('id="cfg-led-refresh-btn"', T('rgbw_scene_refresh')) +
        (isFlash ? cmdBtn('id="cfg-led-loadflash-btn"', T('rgbw_scene_load_flash')) : '') +
        `</div>`;
      let cards = card(T('sidebar_rgbw_scene'), head + `<div class="flasher-config-form">${rows}</div>` + actions);
      if (isFx) {
        // Mode-contextual cards, shown or hidden AS A WHOLE by the device's
        // current effect (scene.visibility — the map's one home).
        if (vis.text_window || vis.text_lines) cards += renderTextCard(L, ch, vis);
        if (vis.text_colors) cards += renderColorsCard(md);
        if (vis.clock) cards += renderClockCard(md);
        if (vis.weather) cards += renderWeatherCard(L, ch, md, vis);
        if (vis.mb_spy) cards += renderSpyCard(L, ch);
      }
      return grid(cards);
    }

    function renderAuxRows(sc, md, ch, vis) {
      const spec = sc.aux_spec || {};
      const f = md.fx_aux_fields || {};
      let rows = '';
      if (vis.fx_aux_low) {
        const lowField = (spec.low_choices && spec.low_choices.length)
          ? selectHtml('cfg-led-aux-low', spec.low_choices, f.low)
          : numberHtml('cfg-led-aux-low', f.low, spec.low_min == null ? 0 : spec.low_min, spec.low_max == null ? 255 : spec.low_max);
        rows += pair('cfg-led-aux-low', T(spec.low_label_key), lowField);
      }
      if (vis.fx_aux_flag) rows += fullRow(checkHtml('cfg-led-aux-flag', !!f.flag, T(spec.flag_label_key)));
      if (vis.fx_aux_color) rows += pair('cfg-led-aux-high', T(spec.high_label_key), selectHtml('cfg-led-aux-high', ch.aux_colors, f.high));
      else if (vis.fx_aux_pool_len) rows += pair('cfg-led-aux-high', T(spec.high_label_key), numberHtml('cfg-led-aux-high', f.high, spec.high_min == null ? 0 : spec.high_min, spec.high_max == null ? 255 : spec.high_max));
      rows += `<div class="cfg-led-pair cfg-led-pair--full"><div class="flasher-config-note cfg-led-hints" id="cfg-led-aux-hint">${auxHintsHtml(sc.aux_hint_keys)}</div></div>`;
      return rows;
    }

    function renderTextCard(L, ch, vis) {
      const maxChars = ch.text_max_chars || 128;
      const text = L.text == null ? '' : String(L.text);
      let rows = '';
      if (vis.text_window) {
        rows += `<div class="cfg-led-pair"><label for="cfg-led-text">${esc('Текст')}</label><div class="cfg-led-inline"><input id="cfg-led-text" data-led-field="1" type="text" maxlength="${esc(maxChars)}" value="${esc(text)}" /><span class="cfg-led-count" id="cfg-led-text-count">${esc(text.length + ' / ' + maxChars)}</span></div></div>`;
        rows += fullRow(note(T('rgbw_text_hint_upper')));
      }
      if (vis.text_lines) {
        rows += pair('cfg-led-textlines', T('rgbw_text_lines'), `<select id="cfg-led-textlines" data-led-field="1">${optionsHtml(ch.text_lines, L.text_lines)}</select>`);
        rows += fullRow(note(fmtHint('rgbw_text_lines_hint_2x', ch.text_2x_hint_args)));
      }
      const actions = vis.text_window
        ? `<div class="flasher-config-actions">${cmdBtn('id="cfg-led-text-apply-btn"', 'Записать текст', 'btn-primary')}</div>`
        : '';
      return card(T('rgbw_text_window'), `<div class="flasher-config-form">${rows}</div>${actions}`);
    }

    function renderColorsCard(md) {
      const hex = md.text_colors_hex || [];
      const rows = TEXT_COLOR_IDS.map((id, i) => pair(id, T(TEXT_COLOR_LABEL_KEYS[i]), colorHtml(id, hex[i]))).join('');
      return card(T('rgbw_card_colors'), `<div class="flasher-config-form">${rows}</div>`);
    }

    function renderClockCard(md) {
      // A sentinel (null) renders BLANK, never 0: 00:00 is a real time and a
      // stray «Записать» would seed midnight.
      const rows = pair('cfg-led-clock-hh', T('rgbw_clock_hh'), numberHtml('cfg-led-clock-hh', md.tod_hours, 0, 23)) +
        pair('cfg-led-clock-mm', T('rgbw_clock_mm'), numberHtml('cfg-led-clock-mm', md.tod_minutes, 0, 59));
      const actions = `<div class="flasher-config-actions">` +
        cmdBtn('id="cfg-led-clock-apply-btn"', 'Записать', 'btn-primary') +
        cmdBtn('id="cfg-led-clock-pc-btn"', T('rgbw_clock_sync_pc')) +
        cmdBtn('id="cfg-led-clock-unset-btn"', T('rgbw_clock_unset')) + `</div>`;
      return card(T('rgbw_card_clock'), `<div class="flasher-config-form">${rows}</div>${actions}`);
    }

    function renderWeatherCard(L, ch, md, vis) {
      const rows = pair('cfg-led-wx-lines', T('rgbw_wx_lines'), `<select id="cfg-led-wx-lines" data-led-field="1">${optionsHtml(ch.wx_lines, L.text_lines)}</select>`) +
        pair('cfg-led-wx-day', T('rgbw_wx_date'), numberHtml('cfg-led-wx-day', md.wx_day, 1, 31), '', false, '(dd)') +
        pair('cfg-led-wx-month', T('rgbw_wx_date'), numberHtml('cfg-led-wx-month', md.wx_month, 1, 12), '', false, '(mm)') +
        pair('cfg-led-wx-temp', T('rgbw_wx_temp'), numberHtml('cfg-led-wx-temp', md.wx_temp_c, -3276.7, 3276.7, 0.1), '', false, '(°C)') +
        pair('cfg-led-wx-hum', T('rgbw_wx_hum'), numberHtml('cfg-led-wx-hum', md.wx_hum_pct, 0, 100, 0.1), '', false, '(%)') +
        pair('cfg-led-wx-press', T('rgbw_wx_press'), numberHtml('cfg-led-wx-press', md.wx_press_mmhg, 400, 850, 0.1), '', false, '(mmHg)') +
        pair('cfg-led-wx-year', T('rgbw_wx_year'), numberHtml('cfg-led-wx-year', md.wx_year, 2000, 2199)) +
        (vis.wx_temp_color ? pair('cfg-led-wx-tempcolor', T('rgbw_wx_temp_color'), colorHtml('cfg-led-wx-tempcolor', md.wx_temp_color_hex)) : '');
      const actions = `<div class="flasher-config-actions">${cmdBtn('id="cfg-led-wx-apply-btn"', 'Применить', 'btn-primary')}</div>`;
      // Weather-listen binds are edited in the spy card only (plan §9 F7): the
      // daemon reads 696..709 for effect 81 alone, and a bind needs the
      // WorkPort set there anyway.
      return card(T('rgbw_card_weather'), `<div class="flasher-config-form">${rows}</div>${actions}`);
    }

    /* ── «Индикатор линии» (spy) card, effect 81 ────────────────────────── */

    function spyLiveText(slot) {
      if (!slot || !slot.uid) return '—';
      return slot.live == null ? '—' : String(slot.live);
    }

    function renderSpyCard(L, ch) {
      const spy = L.spy;
      if (!spy) return card(T('rgbw_card_spy'), empty('Нет данных'));
      let rows = '';
      rows += pair('cfg-led-spy-port', T('rgbw_spy_work_port'), selectHtml('cfg-led-spy-port', ch.spy_ports, spy.port));
      rows += pair('cfg-led-spy-mode', T('rgbw_spy_work_mode'), selectHtml('cfg-led-spy-mode', ch.spy_modes, spy.mode));
      rows += fullRow(checkHtml('cfg-led-spy-tap', !!spy.tap, T('rgbw_spy_tap')));
      rows += pair('cfg-led-spy-lines', T('rgbw_wx_lines'), `<select id="cfg-led-spy-lines" data-led-field="1">${optionsHtml(ch.wx_lines, L.text_lines)}</select>`);
      rows += pair('cfg-led-spy-baud', T('rgbw_spy_baud'), selectHtml('cfg-led-spy-baud', ch.spy_baud, spy.baud, k => k));
      rows += pair('cfg-led-spy-parity', T('rgbw_spy_parity'), selectHtml('cfg-led-spy-parity', ch.spy_parity, spy.parity));
      rows += pair('cfg-led-spy-stop', T('rgbw_spy_stop'), numberHtml('cfg-led-spy-stop', spy.stopbits, 1, 2));
      rows += pair('cfg-led-spy-poll', T('rgbw_spy_poll_ms'), numberHtml('cfg-led-spy-poll', spy.poll_ms, 10, 60000));
      rows += pair('cfg-led-spy-timeout', T('rgbw_spy_timeout_ms'), numberHtml('cfg-led-spy-timeout', spy.timeout_ms, 10, 10000));
      rows += pair('cfg-led-spy-stale', T('rgbw_spy_stale_ms'), numberHtml('cfg-led-spy-stale', spy.stale_ms, 100, 60000));
      rows += `<div class="cfg-led-pair"><span class="cfg-led-group-label">${esc(T('rgbw_spy_status'))}</span><span class="mono" id="cfg-led-spy-status">${esc(spyStatusText(spy.status))}</span></div>`;
      const slots = spy.slots || [];
      const heads = ['rgbw_spy_slot', 'rgbw_spy_uid', 'rgbw_spy_fc', 'rgbw_spy_reg', 'rgbw_spy_type', 'rgbw_spy_decimals', 'rgbw_spy_unit', 'rgbw_spy_lo', 'rgbw_spy_hi', 'rgbw_spy_live'];
      let table = `<table class="cfg-led-table"><thead><tr>${heads.map(k => `<th>${esc(T(k))}</th>`).join('')}</tr></thead><tbody>`;
      for (let n = 1; n <= SPY_SLOT_COUNT; n++) {
        const s = slots[n - 1] || {};
        const p = `cfg-led-spy-s${n}-`;
        table += `<tr><td>${n}</td>` +
          `<td>${numberHtml(p + 'uid', s.uid, 0, 247)}</td>` +
          `<td>${selectHtml(p + 'fc', ch.spy_fc, s.fc)}</td>` +
          `<td>${numberHtml(p + 'reg', s.reg, 0, 65535)}</td>` +
          `<td>${selectHtml(p + 'type', ch.spy_types, s.type)}</td>` +
          `<td>${numberHtml(p + 'dec', s.decimals, 0, 3)}</td>` +
          `<td><input id="${p}unit" data-led-field="1" type="text" maxlength="4" list="cfg-led-spy-units" value="${esc(s.unit == null ? '' : s.unit)}" /></td>` +
          `<td>${numberHtml(p + 'lo', s.lo, -32768, 32767)}</td>` +
          `<td>${numberHtml(p + 'hi', s.hi, -32768, 32767)}</td>` +
          `<td class="mono" id="${p}live">${esc(spyLiveText(s))}</td></tr>`;
      }
      table += `</tbody></table>`;
      table += `<datalist id="cfg-led-spy-units">${(ch.spy_units || []).filter(Boolean).map(u => `<option value="${esc(u)}"></option>`).join('')}</datalist>`;
      const binds = spy.weather || [];
      let bindTable = `<table class="cfg-led-table cfg-led-table--binds"><thead><tr><th></th><th>${esc(T('rgbw_spy_uid'))}</th><th>${esc(T('rgbw_spy_fc'))}</th><th>${esc(T('rgbw_spy_reg'))}</th></tr></thead><tbody>`;
      (ch.wx_spy_fields || []).forEach(function (key, k) {
        const b = binds[k] || {};
        const p = `cfg-led-wxspy-${k}-`;
        bindTable += `<tr><td>${esc(T(key))}</td><td>${numberHtml(p + 'uid', b.uid, 0, 247)}</td><td>${selectHtml(p + 'fc', ch.spy_fc, b.fc)}</td><td>${numberHtml(p + 'reg', b.reg, 0, 65535)}</td></tr>`;
      });
      bindTable += `</tbody></table>`;
      const actions = `<div class="flasher-config-actions">` +
        cmdBtn('id="cfg-led-spy-apply-btn"', 'Применить', 'btn-primary') +
        cmdBtn('id="cfg-led-spy-refresh-btn"', T('rgbw_spy_refresh')) + `</div>`;
      return card(T('rgbw_card_spy'),
        `<div class="cfg-led-subhead">${esc(T('rgbw_spy_params'))}</div>` +
        `<div class="flasher-config-form">${rows}</div>` +
        `<div class="cfg-led-subhead">${esc(T('rgbw_spy_addresses'))}</div>` +
        `<div class="flasher-config-scroll-x">${table}</div>` +
        note(T('rgbw_spy_lim_hint')) + note(T('rgbw_spy_time_hint')) +
        `<div class="cfg-led-subhead">${esc(T('rgbw_wx_spy'))}</div>` +
        `<div class="flasher-config-scroll-x">${bindTable}</div>` +
        note(T('rgbw_wx_spy_hint')) + actions);
    }

    function spyStatusText(status) {
      if (status == null) return '—';
      return '0x' + (Number(status) >>> 0).toString(16).toUpperCase().padStart(4, '0');
    }

    /** The whole spy card as ONE `spy` action — the daemon rewrites 640..646 with
        defaults for anything omitted, so the request always carries every field. */
    function ledSpyParams(snap) {
      const ch = choicesOf(snap) || {};
      const v = id => { const e = el(id); return e ? e.value : null; };
      const on = id => { const e = el(id); return !!(e && e.checked); };
      const slots = [];
      for (let n = 1; n <= SPY_SLOT_COUNT; n++) {
        const p = `cfg-led-spy-s${n}-`;
        slots.push({
          uid: host.clampInt(v(p + 'uid'), 0, 247, 0),
          fc: host.clampInt(v(p + 'fc'), 0, 255, 3),
          reg: host.clampInt(v(p + 'reg'), 0, 65535, 0),
          type: host.clampInt(v(p + 'type'), 0, 255, 0),
          decimals: host.clampInt(v(p + 'dec'), 0, 3, 0),
          unit: String(v(p + 'unit') || ''),
          lo: host.clampInt(v(p + 'lo'), -32768, 32767, 0),
          hi: host.clampInt(v(p + 'hi'), -32768, 32767, 0),
        });
      }
      const weather = (ch.wx_spy_fields || []).map(function (_key, k) {
        const p = `cfg-led-wxspy-${k}-`;
        return {
          uid: host.clampInt(v(p + 'uid'), 0, 247, 0),
          fc: host.clampInt(v(p + 'fc'), 0, 255, 3),
          reg: host.clampInt(v(p + 'reg'), 0, 65535, 0),
        };
      });
      return {
        port: host.clampInt(v('cfg-led-spy-port'), 0, 2, 0),
        mode: host.clampInt(v('cfg-led-spy-mode'), 0, 2, 0),
        tap: on('cfg-led-spy-tap'),
        baud: host.clampInt(v('cfg-led-spy-baud'), 0, 65535, 1152),
        parity: host.clampInt(v('cfg-led-spy-parity'), 0, 2, 0),
        stopbits: host.clampInt(v('cfg-led-spy-stop'), 1, 2, 1),
        poll_ms: host.clampInt(v('cfg-led-spy-poll'), 10, 60000, 1000),
        timeout_ms: host.clampInt(v('cfg-led-spy-timeout'), 10, 10000, 1000),
        stale_ms: host.clampInt(v('cfg-led-spy-stale'), 100, 60000, 10000),
        slots: slots,
        weather: weather,
      };
    }

    function patchSpy(L, set, text) {
      const spy = L.spy;
      if (!spy) return;
      set('cfg-led-spy-port', spy.port);
      set('cfg-led-spy-mode', spy.mode);
      set('cfg-led-spy-tap', !!spy.tap);
      set('cfg-led-spy-lines', L.text_lines);
      set('cfg-led-spy-baud', spy.baud);
      set('cfg-led-spy-parity', spy.parity);
      set('cfg-led-spy-stop', spy.stopbits);
      set('cfg-led-spy-poll', spy.poll_ms);
      set('cfg-led-spy-timeout', spy.timeout_ms);
      set('cfg-led-spy-stale', spy.stale_ms);
      text('cfg-led-spy-status', spyStatusText(spy.status));
      (spy.slots || []).forEach(function (s, i) {
        const p = `cfg-led-spy-s${i + 1}-`;
        set(p + 'uid', s.uid); set(p + 'fc', s.fc); set(p + 'reg', s.reg); set(p + 'type', s.type);
        set(p + 'dec', s.decimals); set(p + 'unit', s.unit); set(p + 'lo', s.lo); set(p + 'hi', s.hi);
        text(p + 'live', spyLiveText(s));
      });
      (spy.weather || []).forEach(function (b, k) {
        const p = `cfg-led-wxspy-${k}-`;
        set(p + 'uid', b.uid); set(p + 'fc', b.fc); set(p + 'reg', b.reg);
      });
    }

    /* ── scene parameter collectors (field names = contract §3) ─────────── */

    function checkedValue(name, pairs) {
      for (const p of pairs || []) {
        const e = el(`cfg-led-${name}-${p[0]}`);
        if (e && e.checked) return String(p[0]);
      }
      return null;
    }

    function ledSceneParams(snap) {
      const L = led(snap);
      const sc = L.scene || {};
      const ch = choicesOf(snap) || {};
      const v = id => { const e = el(id); return e ? e.value : null; };
      const on = id => { const e = el(id); return !!(e && e.checked); };
      const params = {
        source: checkedValue('src', ch.scene_ui) || sc.scene_ui || 'pool',
        fx_id: host.clampInt(v('cfg-led-fx'), 0, (ch.fx_count || 82) - 1, Number(sc.fx_id) || 0),
        fx_speed: host.clampInt(v('cfg-led-speed'), 0, 255, Number(sc.fx_speed) || 0),
        fx_param: host.clampInt(v('cfg-led-bri'), 0, 255, Number(sc.fx_param) || 0),
        loop: el('cfg-led-loop') ? on('cfg-led-loop') : !!sc.loop,
        flash_slot: host.clampInt(v('cfg-led-slot'), 0, 31, Number(sc.flash_slot) || 0),
      };
      if (el('cfg-led-aux-low') || el('cfg-led-aux-flag') || el('cfg-led-aux-high')) {
        params.fx_aux = {
          low: host.clampInt(v('cfg-led-aux-low'), 0, 255, 0),
          flag: on('cfg-led-aux-flag'),
          high: host.clampInt(v('cfg-led-aux-high'), 0, 255, 0),
        };
      }
      if (el('cfg-led-density')) params.fx_density = host.clampInt(v('cfg-led-density'), 0, 255, 128);
      return params;
    }

    function ledTextParams() {
      const params = {};
      const text = el('cfg-led-text');
      if (text) params.text = String(text.value == null ? '' : text.value);
      const lines = el('cfg-led-textlines');
      if (lines) params.lines = host.clampInt(lines.value, 1, 2, 1);
      return params;
    }

    function ledClockParams() {
      const hh = el('cfg-led-clock-hh');
      const mm = el('cfg-led-clock-mm');
      const rawH = hh ? String(hh.value == null ? '' : hh.value).trim() : '';
      const rawM = mm ? String(mm.value == null ? '' : mm.value).trim() : '';
      // Both or nothing: firmware ticks only while both registers are set, and
      // 0 is a real time — a half-filled card must not seed midnight.
      if (!rawH || !rawM) return null;
      return { hours: host.clampInt(rawH, 0, 23, 0), minutes: host.clampInt(rawM, 0, 59, 0) };
    }

    function ledWeatherParams() {
      const v = id => { const e = el(id); return e ? String(e.value == null ? '' : e.value).trim() : ''; };
      const params = {};
      // A blank field is OMITTED: the daemon then writes the firmware sentinel
      // for it, never 0 (0 °C and the 1st of month 0 are real readings).
      const day = v('cfg-led-wx-day'); if (day) params.day = host.clampInt(day, 1, 31, 1);
      const month = v('cfg-led-wx-month'); if (month) params.month = host.clampInt(month, 1, 12, 1);
      const temp = v('cfg-led-wx-temp'); if (temp) params.temp_c = Number(temp.replace(',', '.'));
      const hum = v('cfg-led-wx-hum'); if (hum) params.humidity_pct = Number(hum.replace(',', '.'));
      const press = v('cfg-led-wx-press'); if (press) params.pressure_mmhg = Number(press.replace(',', '.'));
      const year = v('cfg-led-wx-year'); if (year) params.year = host.clampInt(year, 2000, 2199, 2000);
      const color = el('cfg-led-wx-tempcolor');
      if (color) params.temp_color = String(color.value || '');
      return params;
    }

    function patchScene(L, set, text) {
      const sc = L.scene || {};
      const md = L.mode_data || {};
      const ch = L.choices || {};
      (ch.scene_ui || []).forEach(p => set(`cfg-led-src-${p[0]}`, String(p[0]) === String(sc.scene_ui)));
      (sc.fx_groups || []).forEach(g => set(`cfg-led-fxgrp-${g.id}`, g.id === sc.fx_group));
      set('cfg-led-fx', sc.fx_id);
      set('cfg-led-speed', sc.fx_speed);
      set('cfg-led-bri', sc.fx_param);
      set('cfg-led-loop', !!sc.loop);
      set('cfg-led-slot', sc.flash_slot);
      const badge = el('cfg-led-play-badge');
      if (badge) {
        const view = playBadge(sc);
        badge.textContent = view.label;
        badge.className = 'badge ' + view.cls;
      }
      if (L.mode_data) {
        const f = md.fx_aux_fields || {};
        set('cfg-led-density', md.fx_density);
        set('cfg-led-aux-low', f.low);
        set('cfg-led-aux-flag', !!f.flag);
        set('cfg-led-aux-high', f.high);
        const hex = md.text_colors_hex || [];
        TEXT_COLOR_IDS.forEach((id, i) => set(id, hex[i]));
        // null = sentinel ⇒ '' — a blank field, never 0 (rgbw_mode_data_decode).
        set('cfg-led-clock-hh', md.tod_hours);
        set('cfg-led-clock-mm', md.tod_minutes);
        set('cfg-led-wx-day', md.wx_day);
        set('cfg-led-wx-month', md.wx_month);
        set('cfg-led-wx-temp', md.wx_temp_c);
        set('cfg-led-wx-hum', md.wx_hum_pct);
        set('cfg-led-wx-press', md.wx_press_mmhg);
        set('cfg-led-wx-year', md.wx_year);
        set('cfg-led-wx-tempcolor', md.wx_temp_color_hex);
      }
      if (L.text != null) {
        set('cfg-led-text', L.text);
        const cnt = el('cfg-led-text-count');
        const textEl = el('cfg-led-text');
        if (cnt && textEl) cnt.textContent = String(textEl.value == null ? '' : textEl.value).length + ' / ' + (ch.text_max_chars || 128);
      }
      if (L.text_lines != null) {
        set('cfg-led-textlines', L.text_lines);
        set('cfg-led-wx-lines', L.text_lines);
      }
      const hints = el('cfg-led-aux-hint');
      if (hints) {
        const keys = sc.aux_hint_keys || [];
        if (!hints.children || hints.children.length !== keys.length) hints.innerHTML = auxHintsHtml(keys);
        else keys.forEach((k, i) => { const node = hints.children[i]; if (node && node.textContent !== T(k)) node.textContent = T(k); });
      }
      patchSpy(L, set, text);
    }

    function wireScene(body) {
      const snap = currentSnap();
      const sceneWrite = (scope) => ledCommand('scene', ledSceneParams(currentSnap()), scope, T('rgbw_write_scene_ok'));
      body.querySelectorAll('input[name="cfg-led-src"]').forEach(function (r) {
        r.addEventListener('change', () => { if (r.checked) sceneWrite(r); });
      });
      body.querySelectorAll('input[name="cfg-led-fxgrp"]').forEach(function (r) {
        r.addEventListener('change', function () {
          if (!r.checked) return;
          // A new group selects its first effect and writes it — the reply
          // re-renders the picker from the device's state (desktop order).
          const groups = (led(currentSnap()).scene || {}).fx_groups || [];
          const g = groups.find(x => x.id === r.value);
          const params = ledSceneParams(currentSnap());
          if (g) params.fx_id = Number(g.first);
          ledCommand('scene', params, r, T('rgbw_write_scene_ok'));
        });
      });
      ['cfg-led-fx', 'cfg-led-speed', 'cfg-led-bri', 'cfg-led-density', 'cfg-led-aux-low', 'cfg-led-aux-flag', 'cfg-led-aux-high', 'cfg-led-loop', 'cfg-led-slot'].forEach(function (id) {
        const e = body.querySelector('#' + id);
        if (e) e.addEventListener('change', () => sceneWrite(e));
      });
      const play = body.querySelector('#cfg-led-play-btn');
      if (play) play.addEventListener('click', () => ledCommand('play', ledSceneParams(currentSnap()), body, T('rgbw_write_scene_ok')));
      const stop = body.querySelector('#cfg-led-stop-btn');
      if (stop) stop.addEventListener('click', () => ledCommand('stop', {}, body, T('rgbw_write_scene_ok')));
      const refresh = body.querySelector('#cfg-led-refresh-btn');
      if (refresh) refresh.addEventListener('click', () => ledCommand('refresh', {}, body, T('rgbw_write_ok')));
      const load = body.querySelector('#cfg-led-loadflash-btn');
      if (load) load.addEventListener('click', () => ledCommand('load_flash', { slot: host.clampInt((body.querySelector('#cfg-led-slot') || {}).value, 0, 31, 0) }, body, T('rgbw_write_ok')));
      // Marquee text: the counter follows typing; the text lands by its button.
      const text = body.querySelector('#cfg-led-text');
      const count = body.querySelector('#cfg-led-text-count');
      if (text && count) text.addEventListener('input', () => { count.textContent = String(text.value || '').length + ' / ' + ((choicesOf(snap) || {}).text_max_chars || 128); });
      const textApply = body.querySelector('#cfg-led-text-apply-btn');
      if (textApply) textApply.addEventListener('click', () => ledCommand('text', ledTextParams(), textApply.closest('.flasher-config-card') || body, T('rgbw_write_ok')));
      const textLines = body.querySelector('#cfg-led-textlines');
      if (textLines) textLines.addEventListener('change', () => ledCommand('text', { lines: host.clampInt(textLines.value, 1, 2, 1) }, textLines, T('rgbw_write_ok')));
      TEXT_COLOR_IDS.forEach(function (id, i) {
        const c = body.querySelector('#' + id);
        if (!c) return;
        // Colours write on pick (the desktop's behaviour): one register each.
        c.addEventListener('change', function () {
          const colors = {};
          colors[TEXT_COLOR_KEYS[i]] = String(c.value || '');
          ledCommand('text', { colors: colors }, c, T('rgbw_write_ok'));
        });
      });
      const clockApply = body.querySelector('#cfg-led-clock-apply-btn');
      if (clockApply) clockApply.addEventListener('click', function () {
        const params = ledClockParams();
        if (!params) { host.toast('Часы и минуты задаются вместе', 'warn'); return; }
        ledCommand('clock', params, clockApply.closest('.flasher-config-card') || body, T('rgbw_write_ok'));
      });
      const clockPc = body.querySelector('#cfg-led-clock-pc-btn');
      if (clockPc) clockPc.addEventListener('click', () => ledCommand('clock', { from_pc: 1 }, clockPc.closest('.flasher-config-card') || body, T('rgbw_write_ok')));
      const clockUnset = body.querySelector('#cfg-led-clock-unset-btn');
      if (clockUnset) clockUnset.addEventListener('click', () => ledCommand('clock', { unset: 1 }, clockUnset.closest('.flasher-config-card') || body, T('rgbw_write_ok')));
      const wxLines = body.querySelector('#cfg-led-wx-lines');
      if (wxLines) wxLines.addEventListener('change', () => ledCommand('text', { lines: host.clampInt(wxLines.value, 1, 2, 1) }, wxLines, T('rgbw_write_ok')));
      const wxApply = body.querySelector('#cfg-led-wx-apply-btn');
      if (wxApply) wxApply.addEventListener('click', () => ledCommand('weather', ledWeatherParams(), wxApply.closest('.flasher-config-card') || body, T('rgbw_write_ok')));
      const spyLines = body.querySelector('#cfg-led-spy-lines');
      if (spyLines) spyLines.addEventListener('change', () => ledCommand('text', { lines: host.clampInt(spyLines.value, 1, 2, 1) }, spyLines, T('rgbw_write_ok')));
      const spyApply = body.querySelector('#cfg-led-spy-apply-btn');
      if (spyApply) spyApply.addEventListener('click', () => ledCommand('spy', ledSpyParams(currentSnap()), spyApply.closest('.flasher-config-card') || body, T('rgbw_write_ok')));
      const spyRefresh = body.querySelector('#cfg-led-spy-refresh-btn');
      // «Обновить» re-reads the card — no write (the full snapshot of this tab).
      if (spyRefresh) spyRefresh.addEventListener('click', () => host.refreshSnapshot());
    }

    /* ── dispatch: render / patch / key / merge / wire ──────────────────── */

    function render(snap, tab) {
      const t = String(tab || '');
      if (t === LED_TABS[0]) return renderPwmTab(snap);
      if (t === LED_TABS[1]) return renderDiTab(snap);
      if (t === LED_TABS[2]) return renderStripTab(snap);
      if (t === LED_TABS[3]) return renderSceneTab(snap);
      return '';
    }

    /** The operator's field is never overwritten: under focus or edited
        (dataset.ledDirty) it holds until its own write clears the flag. */
    function fieldHolds(e, activeEl) {
      if (!e) return true;
      if (activeEl && e === activeEl) return true;
      return !!(e.dataset && e.dataset.ledDirty === '1');
    }

    function setField(id, value, activeEl) {
      const e = el(id);
      if (!e || fieldHolds(e, activeEl)) return;
      if (e.type === 'checkbox' || e.type === 'radio') { e.checked = !!value; return; }
      const s = value == null ? '' : String(value);
      if (e.value !== s) e.value = s;
      if (e.type === 'range') { const out = el(id + '-val'); if (out) out.textContent = s; }
    }

    function patch(snap) {
      const L = led(snap);
      const tab = String(host.getState().configTab || '');
      const activeEl = host.activeElement ? host.activeElement() : null;
      const set = (id, value) => setField(id, value, activeEl);
      const text = (id, s) => { const e = el(id); if (e && e.textContent !== s) e.textContent = s; };
      if (L.answered === false || !L.choices) return;
      if (tab === LED_TABS[0]) patchPwm(L, set, text);
      else if (tab === LED_TABS[1]) patchDi(L, set);
      else if (tab === LED_TABS[2]) patchStrip(L, set);
      else if (tab === LED_TABS[3]) patchScene(L, set, text);
    }

    /** Same key ⇒ the skeleton is unchanged and the poll patches in place;
        anything that changes WHICH controls exist (the tab, the effect and its
        cards, the render source, the PWM mode, the line mode, an unlisted
        geometry, the spy block arriving) rebuilds the body. */
    function renderKey(snap, tab) {
      if (!snap || snap.kind !== 'led') return '';
      const L = led(snap);
      const sc = L.scene || {};
      const p = L.pwm || {};
      const s = L.strip || {};
      return [
        'led', String(tab || ''),
        L.answered === false ? 'silent' : 'ok',
        L.choices ? 'c' : 'nc',
        sc.fx_id == null ? '' : String(sc.fx_id),
        sc.scene_ui || '',
        p.mode == null ? '' : String(p.mode),
        Array.isArray(p.mode_visible) ? p.mode_visible.map(v => (v ? 1 : 0)).join('') : '',
        s.line_ui || '',
        s.matrix_geometry || '',
        L.spy ? 'spy' : '',
        L.di && Array.isArray(L.di.channels) ? String(L.di.channels.length) : '',
        L.mode_data ? 'md' : '',
        L.text == null ? '' : 't',
      ].join('|');
    }

    /** A reply that did not read an expensive block keeps the previous one
        (the panel poll reads the base block only; a command reply reads the
        active tab's set). A reply that carried the block wins, even if empty.
        An older daemon (no `choices` on a full reply) is named once in the
        banner — the tabs then render «Нет данных» instead of empty controls. */
    function merge(prev, snap) {
      if (!snap || snap.kind !== 'led') return snap;
      const pl = (prev && prev.kind === 'led' && prev.led) || null;
      const cur = Object.assign({}, snap.led || {});
      if (pl) {
        LED_CARRY_KEYS.forEach(function (key) {
          if (cur[key] === undefined && pl[key] !== undefined) cur[key] = pl[key];
        });
      }
      if (!cur.choices && cur.answered !== false && snap.snapshot_detail !== 'panel' && snap.snapshot_detail !== 'stub') {
        if (!pl || !pl.stale_daemon) host.setConfigBanner('Демон прошивальщика старее веб-интерфейса (нет списков выбора) — обновите пакет sa02m-flasher', 'error');
        cur.stale_daemon = true;
      }
      return Object.assign({}, snap, { led: cur });
    }

    function wire(body) {
      if (!body) return;
      body.querySelectorAll('[data-led-field]').forEach(function (e) {
        const mark = function () { e.dataset.ledDirty = '1'; };
        e.addEventListener('input', mark);
        e.addEventListener('change', mark);
        if (e.type === 'range') {
          e.addEventListener('input', function () { const out = el(e.id + '-val'); if (out) out.textContent = e.value; });
        }
      });
      const tab = String(host.getState().configTab || '');
      if (tab === LED_TABS[0]) wirePwm(body);
      else if (tab === LED_TABS[1]) wireDi(body);
      else if (tab === LED_TABS[2]) wireStrip(body);
      else if (tab === LED_TABS[3]) wireScene(body);
    }

    /* ── commands ───────────────────────────────────────────────────────── */

    function setCmdButtons(disabled) {
      const body = el('flasher-config-body');
      if (!body || !body.querySelectorAll) return;
      body.querySelectorAll('[data-led-cmd]').forEach(function (b) { b.disabled = !!disabled; });
    }

    /** Drop the dirty marks inside `scope` (a control, a card, or the body):
        the device's truth is then allowed back in on the next patch. */
    function clearDirty(scope) {
      const root = scope || el('flasher-config-body');
      if (!root) return;
      if (root.dataset) delete root.dataset.ledDirty;
      if (root.querySelectorAll) root.querySelectorAll('[data-led-field]').forEach(function (e) { delete e.dataset.ledDirty; });
    }

    /** One window command → `POST /device_config/led_write`; the reply is the
        fresh snapshot of the active tab and is applied like a poll. Every
        [data-led-cmd] button is disabled and the busy flag set until the reply
        (a second click cannot queue a duplicate batch); the poll generation is
        bumped so an in-flight poll cannot repaint the pre-write state after the
        reply. Errors (400/404/409, the 120 s abort, auth loss) go to the banner
        and a toast — the flasher layer answers real HTTP statuses. */
    async function ledCommand(action, params, scope, okMsg) {
      if (LED_ACTIONS.indexOf(action) < 0) return null;
      const dev = host.currentConfigDevice();
      const port = host.currentPort();
      if (!dev || !port) return null;
      const state = host.getState();
      if (state.configBusy) { host.toast('Дождитесь завершения предыдущей команды', 'warn'); return null; }
      setCmdButtons(true);
      host.setConfigBusy(true);
      host.invalidatePolls();
      try {
        const snap = await host.configApi('/device_config/led_write', {
          port: port,
          device: dev,
          action: action,
          params: params || {},
          active_tab: state.configTab || '',
        });
        clearDirty(scope);
        host.applyConfigSnapshot(snap, true);
        host.setConfigBanner('', '');
        if (okMsg) host.toast(okMsg, 'success');
        return snap;
      } catch (err) {
        const msg = host.t('Команда ленты: ') + (err && err.message ? err.message : String(err));
        clearDirty(scope);
        host.setConfigBanner(msg, 'error');
        host.toast(msg, 'error');
        return null;
      } finally {
        host.setConfigBusy(false);
        setCmdButtons(false);
      }
    }

    return {
      tabs, title, kicker, pollTab, render, patch, renderKey, merge, wire, ledCommand,
      renderPwmTab, renderDiTab, renderStripTab, renderSceneTab, renderSpyCard,
      ledSceneParams, ledStripParams, ledSpyParams, ledTextParams, ledWeatherParams, ledClockParams,
      fieldHolds, T,
    };
  }

  window.sa02mLedWindow = { create: create, LED_T: LED_T, LED_TABS: LED_TABS, LED_ACTIONS: LED_ACTIONS };
})();
