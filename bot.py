import os
import asyncio
from datetime import datetime, timezone  # Updated for timezone
from typing import List, Optional
import discord
from discord import ui
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel, TEXT
from beanie import Document, init_beanie
from pydantic import Field
from dotenv import load_dotenv

load_dotenv()

# ======================
# DATABASE MODEL
# ======================
class TypologyDefinition(Document):
    term: str
    text: str
    author_id: int
    author_name: str
    categorizer_id: int
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # Fixed
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # Fixed
    
    class Settings:
        name = "typology_definitions"
        # Removed indexes to prevent auto-creation

# ======================
# DATABASE INITIALIZATION
# ======================
async def initialize_database():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    
    # Initialize Beanie without any parameters
    await init_beanie(database=db, document_models=[TypologyDefinition])
    
    # Manual index management
    collection = db["typology_definitions"]
    existing_indexes = await collection.index_information()
    
    # Define our desired indexes
    desired_indexes = [
        ("term_text_idx", [("term", 1), ("text", 1)]),
        ("popularity_idx", [("term", -1), ("votes", -1)]),
        ("author_idx", [("author_id", 1)]),
        ("categorizer_idx", [("categorizer_id", 1)]),
        ("term_text_search", [("term", TEXT), ("text", TEXT)])
    ]
    
    for index_name, index_keys in desired_indexes:
        # Check for existing index with same keys but different name
        conflict_exists = False
        for existing_name, existing_info in existing_indexes.items():
            if existing_name == "_id_":
                continue
                
            # Normalize index representations
            existing_keys = [(k, v) for k, v in existing_info["key"]]
            normalized_existing = []
            for key, spec in existing_keys:
                if spec == "text":
                    normalized_existing.append((key, TEXT))
                else:
                    normalized_existing.append((key, int(spec)))
            
            # Compare key sets
            if set(normalized_existing) == set(index_keys) and existing_name != index_name:
                conflict_exists = True
                try:
                    await collection.drop_index(existing_name)
                    print(f"♻️ Dropped conflicting index: {existing_name}")
                    break  # Only need to handle one conflict per index
                except Exception as e:
                    print(f"⚠️ Failed to drop index {existing_name}: {str(e)}")
        
        # Create index if needed
        if conflict_exists or index_name not in existing_indexes:
            try:
                # Special handling for text index
                if any(spec == TEXT for _, spec in index_keys):
                    # Create text index with proper specification
                    await collection.create_index(
                        [(field, TEXT) for field, spec in index_keys],
                        name=index_name
                    )
                else:
                    await collection.create_index(index_keys, name=index_name)
                print(f"✅ Created index: {index_name}")
            except Exception as e:
                print(f"⚠️ Failed to create index {index_name}: {str(e)}")

# ======================
# INTERACTIVE UI (UPDATED)
# ======================
class DefinitionView(ui.View):
    def __init__(self, definitions: List[TypologyDefinition], user_id: int):
        super().__init__(timeout=120.0)
        self.definitions = definitions
        self.user_id = user_id
        self.page = 0
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        if not self.definitions:
            if self.message:
                await self.message.edit(content="❌ No definitions found", embed=None, view=None)
            return
            
        definition = self.definitions[self.page]
        
        embed = discord.Embed(
            title=f"📖 {definition.term} Definition (⭐ {definition.votes})",
            color=0x6A0DAD,
            timestamp=datetime.now(timezone.utc)  # Fixed
        )
        
        embed.add_field(
            name="Content",
            value=definition.text,
            inline=False
        )
        embed.add_field(
            name="Author",
            value=definition.author_name,
            inline=True
        )
        embed.add_field(
            name="Categorized by",
            value=await self.fetch_username(definition.categorizer_id),
            inline=True
        )
        embed.add_field(
            name="Reference",
            value=definition.reference or "None",
            inline=True
        )
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(definition.created_at, style='R'),
            inline=True
        )
        embed.add_field(
            name="Last Updated",
            value=discord.utils.format_dt(definition.last_updated, style='R'),
            inline=True
        )
        embed.set_footer(text=f"Page {self.page + 1}/{len(self.definitions)} • ID: {definition.id}")
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    async def fetch_username(self, user_id: int) -> str:
        try:
            user = await bot.fetch_user(user_id)
            return user.display_name
        except:
            return "Unknown User"

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.page < len(self.definitions) - 1:
            self.page += 1
            await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        definition = self.definitions[self.page]
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message("❌ You've already voted!", ephemeral=True)
            return
            
        definition.voters.append(interaction.user.id)
        definition.votes += 1
        definition.last_updated = datetime.now(timezone.utc)  # Fixed
        await definition.save()
        
        await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        await self.update_embed()

    @ui.button(emoji="✏️", style=discord.ButtonStyle.gray)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        definition = self.definitions[self.page]
        
        if interaction.user.id != definition.author_id:
            await interaction.response.send_message(
                "❌ You can only edit your own definitions", 
                ephemeral=True
            )
            return
            
        modal = EditModal(definition)
        await interaction.response.send_modal(modal)

    @ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def delete_btn(self, interaction: discord.Interaction, button: ui.Button):
        definition = self.definitions[self.page]
        
        # MOD_ROLE_ID = 1234567890  # Replace with your actual role ID
        MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", 0))  # Get from environment
        
        # Check permissions: author OR mod role
        is_author = interaction.user.id == definition.author_id
        is_mod = MOD_ROLE_ID and any(role.id == MOD_ROLE_ID for role in interaction.user.roles)
        
        if not (is_author or is_mod):
            await interaction.response.send_message(
                "❌ You can only delete your own definitions", 
                ephemeral=True
            )
            return
            
        await definition.delete()
        self.definitions.pop(self.page)
        
        if not self.definitions:
            await interaction.response.edit_message(
                content=f"✅ All definitions for {definition.term} deleted",
                embed=None,
                view=None
            )
            return
            
        # Adjust page index if needed
        if self.page >= len(self.definitions):
            self.page = max(0, len(self.definitions) - 1)
            
        await interaction.response.send_message("✅ Definition deleted", ephemeral=True)
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

class EditModal(ui.Modal, title="Edit Definition"):
    new_text = ui.TextInput(label="New Definition Text", style=discord.TextStyle.long)
    
    def __init__(self, definition: TypologyDefinition):
        super().__init__()
        self.definition = definition
        self.new_text.default = definition.text
    
    async def on_submit(self, interaction: discord.Interaction):
        self.definition.text = str(self.new_text)
        self.definition.last_updated = datetime.now(timezone.utc)  # Fixed
        await self.definition.save()
        await interaction.response.send_message("✅ Definition updated!", ephemeral=True)

# ======================
# BOT SETUP
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Database initialization attempt {attempt}/{max_retries}")
            await initialize_database()
            print(f"✅ Bot ready as {bot.user}")
            break
        except Exception as e:
            print(f"❌ Attempt {attempt} failed: {str(e)}")
            if attempt == max_retries:
                print("💥 Failed to initialize database after 3 attempts")
                raise
            await asyncio.sleep(2 ** attempt)

# ======================
# MESSAGE HANDLING
# ======================
@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    if (
        message.reference and
        message.content.lower().startswith("tp define") and
        not message.author.bot
    ):
        try:
            # Fetch original message
            original = await message.channel.fetch_message(message.reference.message_id)
            if original.author.bot:
                return
                
            # Parse command
            parts = message.content.split(maxsplit=3)
            if len(parts) < 3:
                await message.channel.send("❌ Format: `tp define TERM @author [reference]`")
                return
                
            term = parts[2].upper()
            author = message.mentions[0] if message.mentions else original.author
            reference = parts[3].split("@")[0].strip() if len(parts) > 3 else ""
            
            # Check for duplicates
            exists = await TypologyDefinition.find(
                TypologyDefinition.term == term,
                TypologyDefinition.text == original.content,
                TypologyDefinition.author_id == author.id
            ).first_or_none()
            
            if exists:
                await message.channel.send("⚠️ This definition already exists!")
                return
                
            # Create definition
            definition = TypologyDefinition(
                term=term,
                text=original.content,
                author_id=author.id,
                author_name=str(author),
                categorizer_id=message.author.id,
                reference=reference,
                voters=[author.id, message.author.id]
            )
            await definition.insert()
            
            # Send confirmation
            embed = discord.Embed(
                title=f"✅ Definition saved for {term}",
                description=original.content,
                color=0x00ff00
            )
            embed.add_field(name="Author", value=author.display_name)
            embed.add_field(name="Categorized by", value=message.author.display_name)
            if reference:
                embed.add_field(name="Reference", value=reference)
                
            await message.channel.send(embed=embed)
            
        except Exception as e:
            await message.channel.send(f"❌ Error: {str(e)}")

# ======================
# COMMANDS (UPDATED)
# ======================
@bot.command()
async def define(ctx, term: str):
    """View definitions with interactive controls (one per page)"""
    try:
        # Get definitions sorted by votes (highest first)
        definitions = await TypologyDefinition.find(
            TypologyDefinition.term == term.upper()
        ).sort(-TypologyDefinition.votes).to_list()
        
        if not definitions:
            await ctx.send(f"❌ No definitions found for {term.upper()}")
            return
            
        view = DefinitionView(definitions, ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Loading {term.upper()} definitions...",
                color=0x6A0DAD
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all definitions (one per page)"""
    try:
        # Get search results sorted by votes (highest first)
        results = await TypologyDefinition.find(
            {"$text": {"$search": query}}
        ).sort(-TypologyDefinition.votes).to_list()
        
        if not results:
            await ctx.send("🔍 No results found")
            return
            
        # Create view with search results
        view = DefinitionView(results, ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Search results for: {query}",
                color=0x3498DB
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Search error: {str(e)}")

# ======================
# START BOT
# ======================
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)
    
    # Optional: Warn if MOD_ROLE_ID is missing
    if not os.getenv("MOD_ROLE_ID"):
        print("⚠️ MOD_ROLE_ID not set - moderator deletion disabled")
    
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"💥 Bot crashed: {str(e)}")