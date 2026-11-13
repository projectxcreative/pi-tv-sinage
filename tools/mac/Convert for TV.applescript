-- Convert for TV
-- Drop videos on this app (or double-click it and choose them) to convert them
-- to 4K H.265 (HEVC), which the Raspberry Pi 4 plays smoothly. Uses macOS's
-- built-in converter (avconvert) and the Mac's hardware encoder, so nothing
-- needs installing. The converted copy is saved next to the original as
-- "<name> (TV 4K HEVC).mp4"; the original is not changed.
--
-- Build:  osacompile -o "Convert for TV.app" "Convert for TV.applescript"

property presetName : "PresetHEVC3840x2160"
property suffix : " (TV 4K HEVC).mp4"
property videoExtensions : {"mp4", "m4v", "mov", "mkv", "avi", "mts", "m2ts", "ts", "mxf"}

on run
	set picked to choose file with prompt "Choose videos to convert for the TV:" of type {"public.movie"} with multiple selections allowed
	convertFiles(picked)
end run

on open droppedItems
	convertFiles(droppedItems)
end open

on convertFiles(theItems)
	set videos to {}
	repeat with f in theItems
		set p to POSIX path of f
		if isVideo(p) and p does not end with suffix then set end of videos to p
	end repeat
	if (count of videos) is 0 then
		display dialog "Drop video files (MP4, MOV, M4V…) on Convert for TV." buttons {"OK"} default button 1 with icon caution
		return
	end if

	set total to count of videos
	set progress total steps to total
	set progress completed steps to 0
	set done to {}
	set failed to {}
	repeat with i from 1 to total
		set src to item i of videos
		set outPath to outputPathFor(src)
		set progress description to "Converting " & i & " of " & total & " for the TV…"
		set progress additional description to baseName(src) & return & "4K videos take roughly half their running time."
		try
			do shell script "/usr/bin/avconvert --preset " & presetName & " --source " & quoted form of src & ¬
				" --output " & quoted form of outPath & " --replace 2>&1"
			set end of done to baseName(outPath)
		on error errMsg
			do shell script "rm -f " & quoted form of outPath
			set end of failed to baseName(src) & ": " & errMsg
		end try
		set progress completed steps to i
	end repeat

	set msg to ""
	if (count of done) > 0 then
		set msg to "Ready to upload to the TV:" & return & joinLines(done)
	end if
	if (count of failed) > 0 then
		set msg to msg & return & return & "Couldn't convert:" & return & joinLines(failed)
	end if
	display notification "Converted " & (count of done) & " of " & total & " videos" with title "Convert for TV"
	set reply to button returned of (display dialog msg buttons {"Show in Finder", "Done"} default button "Done")
	if reply is "Show in Finder" and (count of done) > 0 then
		do shell script "open -R " & quoted form of outputPathFor(item 1 of videos)
	end if
end convertFiles

on isVideo(p)
	set ext to do shell script "f=" & quoted form of p & "; e=\"${f##*.}\"; echo \"$e\" | tr '[:upper:]' '[:lower:]'"
	return videoExtensions contains ext
end isVideo

on outputPathFor(p)
	return (do shell script "f=" & quoted form of p & "; echo \"${f%.*}\"") & suffix
end outputPathFor

on baseName(p)
	return do shell script "basename " & quoted form of p
end baseName

on joinLines(theList)
	set AppleScript's text item delimiters to return
	set s to theList as text
	set AppleScript's text item delimiters to ""
	return s
end joinLines
