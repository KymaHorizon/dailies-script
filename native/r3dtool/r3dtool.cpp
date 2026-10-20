// SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
// SPDX-License-Identifier: Apache-2.0

// r3dtool: a small command-line bridge between the dailies pipeline and the RED R3D SDK.
//
//   r3dtool version           SDK and tool version as JSON
//   r3dtool meta <clip.R3D>   clip properties and every metadata item as JSON
//
// The pipeline never links the SDK. It runs this tool through the command
// builder like any other external command, so every call is logged verbatim.
// Address the first file of a spanned clip. The SDK follows the span itself.
//
// The SDK dynamic libraries must sit beside this executable, as RED's notes
// require. Set R3DTOOL_LIBDIR to override. Nothing from the SDK is stored in
// this repository: build.sh takes the SDK location from R3DSDK_ROOT.
//
// Exit codes: 0 ok, 2 usage, 3 SDK failed to initialise, 4 clip failed to load.

#include <R3DSDK.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#else
#include <unistd.h>
#endif

static const char* kToolVersion = "0.1.0";

static std::string executable_dir()
{
	char buffer[4096];
#if defined(__APPLE__)
	uint32_t size = sizeof(buffer);
	if (_NSGetExecutablePath(buffer, &size) != 0) return ".";
#else
	ssize_t length = readlink("/proc/self/exe", buffer, sizeof(buffer) - 1);
	if (length <= 0) return ".";
	buffer[length] = '\0';
#endif
	char resolved[4096];
	const char* path = realpath(buffer, resolved) ? resolved : buffer;
	std::string dir(path);
	size_t slash = dir.find_last_of('/');
	return slash == std::string::npos ? "." : dir.substr(0, slash);
}

static std::string json_string(const std::string& text)
{
	std::string out = "\"";
	for (unsigned char c : text)
	{
		switch (c)
		{
		case '"': out += "\\\""; break;
		case '\\': out += "\\\\"; break;
		case '\n': out += "\\n"; break;
		case '\r': out += "\\r"; break;
		case '\t': out += "\\t"; break;
		default:
			if (c < 0x20)
			{
				char escaped[8];
				snprintf(escaped, sizeof(escaped), "\\u%04x", c);
				out += escaped;
			}
			else out += static_cast<char>(c);
		}
	}
	return out + "\"";
}

static std::string json_number(double value)
{
	if (!std::isfinite(value)) return "null";
	char buffer[64];
	snprintf(buffer, sizeof(buffer), "%.9g", value);
	return buffer;
}

static std::string json_tc(const char* timecode)
{
	return timecode ? json_string(timecode) : "null";
}

// SDK objects live in this scope, which is deeper than the one that calls
// InitializeSdk and FinalizeSdk, as the SDK notes require.
static int print_meta(const char* path)
{
	R3DSDK::Clip clip(path);
	if (clip.Status() != R3DSDK::LSClipLoaded)
	{
		fprintf(stderr, "r3dtool: failed to load clip, status %d: %s\n", static_cast<int>(clip.Status()), path);
		return 4;
	}
	const size_t frames = clip.VideoFrameCount();
	std::string out = "{";
	out += "\"path\":" + json_string(path);
	out += ",\"width\":" + std::to_string(clip.Width());
	out += ",\"height\":" + std::to_string(clip.Height());
	out += ",\"frames\":" + std::to_string(frames);
	out += ",\"fps\":" + json_number(clip.VideoAudioFramerate());
	out += ",\"audio_channels\":" + std::to_string(clip.AudioChannelCount());
	if (frames > 0)
	{
		out += ",\"tc_default\":[" + json_tc(clip.Timecode(0)) + "," + json_tc(clip.Timecode(frames - 1)) + "]";
		out += ",\"tc_absolute\":[" + json_tc(clip.AbsoluteTimecode(0)) + "," + json_tc(clip.AbsoluteTimecode(frames - 1)) + "]";
		out += ",\"tc_edge\":[" + json_tc(clip.EdgeTimecode(0)) + "," + json_tc(clip.EdgeTimecode(frames - 1)) + "]";
	}
	out += ",\"metadata\":{";
	for (size_t i = 0; i < clip.MetadataCount(); ++i)
	{
		if (i) out += ",";
		out += json_string(clip.MetadataItemKey(i)) + ":";
		switch (clip.MetadataItemType(i))
		{
		case R3DSDK::MetadataTypeInt: out += std::to_string(clip.MetadataItemAsInt(i)); break;
		case R3DSDK::MetadataTypeFloat: out += json_number(clip.MetadataItemAsFloat(i)); break;
		case R3DSDK::MetadataTypeString: out += json_string(clip.MetadataItemAsString(i)); break;
		default: out += "null"; break;
		}
	}
	out += "}}";
	puts(out.c_str());
	return 0;
}

int main(int argc, char** argv)
{
	const bool want_version = argc == 2 && strcmp(argv[1], "version") == 0;
	const bool want_meta = argc == 3 && strcmp(argv[1], "meta") == 0;
	if (!want_version && !want_meta)
	{
		fprintf(stderr, "usage: r3dtool version | r3dtool meta <clip.R3D>\n");
		return 2;
	}

	const char* override_dir = getenv("R3DTOOL_LIBDIR");
	const std::string libdir = override_dir ? override_dir : executable_dir();
	const R3DSDK::InitializeStatus status = R3DSDK::InitializeSdk(libdir.c_str(), OPTION_RED_NONE);
	if (status != R3DSDK::ISInitializeOK)
	{
		fprintf(stderr, "r3dtool: R3D SDK failed to initialise, status %d, libraries expected in %s\n",
		        static_cast<int>(status), libdir.c_str());
		return 3;
	}

	int result = 0;
	if (want_version)
	{
		printf("{\"tool\":\"r3dtool\",\"tool_version\":%s,\"sdk\":%s}\n", json_string(kToolVersion).c_str(),
		       json_string(R3DSDK::GetSdkVersion()).c_str());
	}
	else
	{
		result = print_meta(argv[2]);
	}
	R3DSDK::FinalizeSdk();
	return result;
}
