import discord
from discord import app_commands

class PingCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="ping", description="Replies with pong!")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def ping(interaction: discord.Interaction):
            latency_ms = int(interaction.client.latency * 1000)
            await interaction.response.send_message(f"Pong! `{latency_ms}ms`", ephemeral=True)
