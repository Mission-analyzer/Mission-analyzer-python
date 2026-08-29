-- snake-sine-plane.lua -- синусоїдальна "змійка" для ArduPlane через
-- NAV_SCRIPT_TIME (той самий механізм, що й офіційний приклад
-- copter-nav-script-time.lua, лише геометрія руху інша: не квадрат,
-- а безперервна синусоїда навколо базового курсу).
--
-- === ЩО РОБИТЬ ===
-- Літак відхиляється то ліворуч, то праворуч від прямого курсу за
-- синусоїдою (не різкі повороти, плавна хвиля), потім повертається на
-- прямий курс і місія продовжується як звичайно.
--
-- === ПАРАМЕТРИ КОМАНДИ MAV_CMD_NAV_SCRIPT_TIME (42702) ===
-- Ця ОДНА команда MAVLink використовується ArduPilot для БУДЬ-ЯКОЇ
-- Lua-логіки в місії -- сам протокол не розрізняє "які саме" скрипти
-- за нею стоять, тому param1 ("command") -- довільне число, яке
-- ІНТЕРПРЕТУЄ САМЕ ЦЕЙ скрипт (не стандарт MAVLink):
--
--   param1 ("command")  = 80  -- СТАРТ змійки
--                        = 81 -- запобіжник аварійного завершення
--   param2 ("timeout")  = тривалість маневру, секунд (>0, обов'язково)
--   param3 ("arg1")     = амплітуда бокового відхилення, метрів
--   param4 ("arg2")     = період однієї повної хвилі, секунд
--
-- === ЯК ВБУДУВАТИ В МІСІЮ ===
-- Приклад повної місії (людською мовою):
--   0. Home
--   1. TAKEOFF
--   2. NAV_WAYPOINT -- вихід на робочий курс (напрямок ЦЬОГО відрізка
--      стане базовим курсом змійки -- літак іде по прямій сюди, і
--      САМЕ В ЦЬОМУ напрямку почне звивистий рух після)
--   3. NAV_SCRIPT_TIME -- сам маневр змійки, з параметрами вище
--   4. RETURN_TO_LAUNCH (чи наступний звичайний пункт)
--
-- Той самий пункт 3 у файлі .waypoints (формат QGC WPL 110, той самий,
-- що читає Mission Analyzer) -- amplitude=40м, period=20с, timeout=60с:
--
--   3	0	3	42702	80	60	40	20	0	0	0	1
--   (колонки: index  current  frame  command  param1  param2  param3
--    param4  lat  lon  alt  autocontinue -- lat/lon/alt тут не мають
--    сенсу для цієї команди, лишаються нулями)
--
-- В Mission Analyzer: поки NAV_SCRIPT_TIME НЕМАЄ в списку діалогу
-- "Додати команду" (перевір актуальність у changelog/README проєкту)
-- -- рядок вище можна дописати в .waypoints вручну текстовим
-- редактором до завантаження файлу в програму.
--
-- === ВИМОГИ ===
-- - ArduPlane з увімкненим скриптингом: параметр SCR_ENABLE=1,
--   перезавантаження плати.
-- - vehicle:set_target_location() для Plane -- зʼявилось не одразу
--   (ArduPilot issue #13666), потрібна відносно сучасна прошивка.
-- - Режим AUTO, вейпоінт ПЕРЕД NAV_SCRIPT_TIME повинен реально
--   задавати потрібний напрямок польоту (курс саме на нього стає
--   базовим для змійки).
--
-- Команда 81 -- аварійне повернення керування місії (запобіжник --
-- скрипт НЕ може перервати вже виконувану команду 80 через звичайну
-- послідовність пунктів місії, оскільки NAV_SCRIPT_TIME виконується
-- строго по черзі й чекає на done(); 81 тут -- захист на випадок
-- повторного виклику з running=true, а не гарантований "стоп" ззовні).

local CMD_SNAKE_START = 80
local CMD_SNAKE_ABORT = 81
local UPDATE_PERIOD_MS = 200  -- 5Гц -- досить для плавної синусоїди на літаку, не так критично, як пряме керування rate/attitude
local LOOKAHEAD_M = 60        -- ціль -- завжди на цю відстань попереду поточної позиції ("морквина на палиці", guided-режим)

local running = false
local last_id = -1
local base_course_rad = 0     -- курс (yaw) на момент старту, радіани
local start_time_ms = 0
local amplitude_m = 0
local period_s = 20

local function wrap_2pi(a)
    while a < 0 do a = a + 2 * math.pi end
    while a >= 2 * math.pi do a = a - 2 * math.pi end
    return a
end

function update()
    local id, cmd, arg1, arg2, arg3, arg4 = vehicle:nav_script_time()

    if id then
        if id ~= last_id then
            -- новий NAV_SCRIPT_TIME -- вирішуємо, що робити, за cmd
            last_id = id

            if cmd == CMD_SNAKE_START then
                local yaw = ahrs:get_yaw()
                if yaw then
                    base_course_rad = yaw
                    start_time_ms = millis():toint()
                    amplitude_m = arg2 or 0   -- param3 -> arg1 у API нумерації, див. приклад ArduPilot (arg1..arg4 == param1..param4 команди)
                    period_s = arg3 or 20     -- param4
                    running = true
                    gcs:send_text(0, string.format(
                        "snake: старт, ampl=%.1fm period=%.1fs timeout=%.0fs", amplitude_m, period_s, arg1 or 0))
                else
                    gcs:send_text(0, "snake: не вдалось отримати курс (yaw)")
                    vehicle:nav_script_time_done(id)
                    running = false
                end

            elseif cmd == CMD_SNAKE_ABORT then
                -- запобіжник: якщо чомусь ще "running" з попереднього
                -- виклику -- негайно завершуємо й повертаємо керування
                if running then
                    gcs:send_text(0, "snake: abort (cmd=81)")
                end
                running = false
                vehicle:nav_script_time_done(id)

            else
                -- невідома команда для цього скрипта -- одразу
                -- повертаємо керування, щоб місія не зависла
                vehicle:nav_script_time_done(id)
            end
        end

        if running then
            local timeout_s = arg1 or 0
            local elapsed_s = (millis():toint() - start_time_ms) / 1000.0

            if timeout_s > 0 and elapsed_s >= timeout_s then
                gcs:send_text(0, "snake: timeout, завершення")
                vehicle:nav_script_time_done(last_id)
                running = false
            else
                local curr_loc = ahrs:get_location()
                if curr_loc then
                    -- фаза синусоїди -- за часом, не за пройденою відстанню
                    local phase = 2 * math.pi * elapsed_s / math.max(period_s, 1)
                    local lateral_m = amplitude_m * math.sin(phase)

                    -- вперед уздовж базового курсу (bearing 0=Північ, 90=Схід)
                    local fwd_n = LOOKAHEAD_M * math.cos(base_course_rad)
                    local fwd_e = LOOKAHEAD_M * math.sin(base_course_rad)

                    -- перпендикулярно (90° праворуч від курсу) -- бокове відхилення
                    local perp_rad = wrap_2pi(base_course_rad + math.pi / 2)
                    local lat_n = lateral_m * math.cos(perp_rad)
                    local lat_e = lateral_m * math.sin(perp_rad)

                    local target_loc = curr_loc:copy()
                    target_loc:offset(fwd_n + lat_n, fwd_e + lat_e)

                    if not vehicle:set_target_location(target_loc) then
                        gcs:send_text(0, "snake: set_target_location не вдалось")
                        vehicle:nav_script_time_done(last_id)
                        running = false
                    end
                else
                    gcs:send_text(0, "snake: не вдалось отримати поточну позицію")
                end
            end
        end
    else
        running = false
    end

    return update, UPDATE_PERIOD_MS
end

return update()
