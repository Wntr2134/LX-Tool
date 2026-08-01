-- XBridge: push executor labels to the X-Touch scribble strips.
--
-- Sends /xbridge/label/<strip>,s,<name> for the executors the bridge's
-- faders ride (201-208 on the current page by default), via the console's
-- OSC line, every few seconds - so the strips show what the executors are
-- actually called instead of just their numbers.
--
-- Install: Menu > Show Creator > Import > Plugin, or drop this file into
-- the gma3_library/datapools/plugins folder and import it, then run it
-- (assign to a macro or run once per session - it keeps itself alive).
--
-- Adjust these two lines if your mapping differs:
local EXECUTORS = {201, 202, 203, 204, 205, 206, 207, 208}
local OSC_LINE = 1   -- the OSC line number in In & Out > OSC

-- STATUS: experimental - written against the grandMA3 v2.x Lua API but
-- not yet run on a console. If names don't appear, run it from the
-- command line as a plugin and check the System Monitor for errors;
-- the GetExecutor call is the part most likely to need a version tweak.

local function labelOf(execNo)
    local ok, exec = pcall(GetExecutor, execNo)
    if not ok or exec == nil then
        return ""
    end
    local obj = exec.Object
    if obj == nil then
        return ""
    end
    local name = obj.name or obj:GetUIName() or ""
    return tostring(name)
end

local function sendLabels()
    for strip, execNo in ipairs(EXECUTORS) do
        local name = labelOf(execNo)
        if name ~= "" then
            -- Escape commas: they separate OSC arguments in SendOSC.
            name = string.gsub(name, ",", "\\,")
            Cmd(string.format('SendOSC %d "/xbridge/label/%d,s,%s"',
                              OSC_LINE, strip, string.sub(name, 1, 12)))
        end
    end
end

local function main()
    Printf("XBridge labels: pushing executor names every 5s (plugin keeps running)")
    while true do
        sendLabels()
        coroutine.yield(5)   -- re-run in 5 seconds
    end
end

return main
