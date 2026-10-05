import edge_tts
import asyncio

async def main():
    voices = await edge_tts.list_voices()
    vi_voices = [v for v in voices if v['Locale'] == 'vi-VN']
    print(f"Found {len(vi_voices)} Vietnamese voices:\n")
    for v in vi_voices:
        personalities = v.get('VoiceTag', {}).get('VoicePersonalities', 'N/A')
        content_cats = v.get('VoiceTag', {}).get('ContentCategories', 'N/A')
        print(f"  {v['ShortName']:30s} | {v['Gender']:8s} | Personalities: {personalities}")
        print(f"  {'':30s} | {'':8s} | Categories: {content_cats}")
        print()

asyncio.run(main())
